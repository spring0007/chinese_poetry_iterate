# -*- coding: utf-8 -*-
"""
HTTP JSON 接口（标准库实现，零第三方依赖）。

why 用 http.server 而不是 FastAPI/Flask：这个产线的目标之一是"多平台接入"，
服务端只是其中一种接法。用标准库意味着任何装了 Python 的机器 python api_server.py
就能跑起来，不用先解决依赖问题。要上生产再换成 FastAPI/uvicorn，路由逻辑一行不用改。

启动：
    python examples/api_server.py --db ../dist/poetry.db --port 8787
    python examples/api_server.py --db ../dist/poetry.db \
        --strains-db ../dist/poetry-strains.db --allow-write

只读模式是默认（--allow-write 才开写接口）：
    对外暴露的服务默认不该能改数据。写接口单独开关，是刻意的安全默认值。

接口：
    GET  /api/health
    GET  /api/stats
    GET  /api/search?q=明月&dynasty=tang&kind=poem&author=李白&limit=10&offset=0
    GET  /api/poem/<id>                  详情（挂了平仄包则带 strains）
    GET  /api/authors?q=李&limit=20      作者联想
    GET  /api/rhythmics?q=水调          词牌联想
    GET  /api/mine?limit=50              自建条目
    POST /api/poem                       新增（需 --allow-write）
    DELETE /api/poem/<id>                删除（需 --allow-write，且仅限自建条目）

依赖: Python 3.11+
"""

from __future__ import annotations
import os, sys, json, argparse, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import schema as S
import query as Q
import store as ST

# 单次响应条数上限。不设的话 ?limit=999999 会把整个库读进内存再序列化。
MAX_LIMIT = 200
MAX_BODY = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "poetry-api/2.0"
    # 慢客户端不能一直占着线程
    timeout = 30

    # ---------------------------------------------------------------- 工具
    def _send(self, code: int, obj, cors: bool = True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code: int, msg: str):
        self._send(code, {"error": msg})

    def _qs(self) -> dict:
        u = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(u.query)

    def log_message(self, fmt, *args):
        # 默认会把每个请求打到 stderr，刷屏；只在出错时打
        if self.server.debug:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- 路由
    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        # 每个请求开自己的只读连接。why：ThreadingHTTPServer 是多线程的，
        # 而 sqlite3 默认禁止跨线程复用同一个连接对象（check_same_thread）。
        # 共用连接会随机抛 ProgrammingError；每请求开连接的开销只有几十微秒。
        con = ST.open_ro(self.server.db, self.server.strains_db)
        try:
            self._get(con)
        except Exception as e:
            self._fail(500, "%s: %s" % (type(e).__name__, e))
        finally:
            con.close()

    def _get(self, con):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/")
        qs = self._qs()

        def one(k, default=""):
            v = qs.get(k, [default])[0]
            return v

        def as_int(k, default, lo, hi):
            try:
                v = int(one(k, str(default)))
            except ValueError:
                return default
            return max(lo, min(hi, v))

        if path in ("/api/health", "/health"):
            return self._send(200, {"ok": True, "schema": S.SCHEMA_VERSION,
                                    "write": self.server.allow_write,
                                    "strains": Q.has_strains(con)})

        if path == "/api/stats":
            return self._send(200, ST.stats(con))

        if path == "/api/search":
            q = one("q")
            author = one("author")
            rhy = one("rhythmic")
            limit = as_int("limit", 10, 1, MAX_LIMIT)
            offset = as_int("offset", 0, 0, 1_000_000)
            if not q and not author and not rhy:
                return self._fail(400, "q / author / rhythmic 至少填一个")
            if rhy and not q:
                rows = Q.by_rhythmic(con, rhy, limit)
            elif author and not q:
                rows = Q.by_author(con, author, limit)
            else:
                rows = Q.search(con, q, one("dynasty"), one("kind"), author,
                                limit, offset)
            return self._send(200, {"total": len(rows), "items": [_pub(r) for r in rows]})

        if path == "/api/authors":
            q = one("q")
            limit = as_int("limit", 20, 1, MAX_LIMIT)
            like = "%" + S.norm_text(q) + "%" if q else "%"
            rows = con.execute(
                "SELECT id,name,dynasty,n_poems FROM authors WHERE name LIKE ? "
                "ORDER BY n_poems DESC LIMIT ?", (like, limit)).fetchall()
            return self._send(200, {"items": [dict(r) for r in rows]})

        if path == "/api/rhythmics":
            q = one("q")
            limit = as_int("limit", 20, 1, MAX_LIMIT)
            like = "%" + S.norm_text(q) + "%" if q else "%"
            rows = con.execute(
                "SELECT id,name FROM rhythmics WHERE name LIKE ? LIMIT ?",
                (like, limit)).fetchall()
            return self._send(200, {"items": [dict(r) for r in rows]})

        if path == "/api/mine":
            limit = as_int("limit", 50, 1, MAX_LIMIT)
            rows = ST.list_user_added(con, limit)
            return self._send(200, {"items": [_pub(r) for r in rows]})

        if path.startswith("/api/poem/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._fail(400, "id 必须是整数")
            r = Q.get_by_id(con, pid)
            if not r:
                return self._fail(404, "未找到 id=%d" % pid)
            return self._send(200, _pub(r))

        self._fail(404, "未知路径 %s（见 examples/README.md）" % path)

    # ---------------------------------------------------------------- 写
    def do_POST(self):
        if not self.server.allow_write:
            # 只读模式下先读 Content-Length 把请求体抽走，否则客户端会收到连接重置
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
            except ValueError:
                pass
            return self._fail(403, "服务为只读模式，启动时加 --allow-write 才开放写接口")
        try:
            self._post()
        except Exception as e:
            self._fail(500, "%s: %s" % (type(e).__name__, e))

    def _post(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.rstrip("/") != "/api/poem":
            return self._fail(404, "未知路径")
        payload = self._read_json()
        if payload is None:
            return
        con = ST.open_rw(self.server.db, self.server.strains_db)
        try:
            r = ST.add_poem(
                con,
                dynasty=payload.get("dynasty", "custom"),
                kind=payload.get("kind", "poem"),
                title=payload.get("title", ""),
                author=payload.get("author", ""),
                lines=payload.get("lines") or [],
                rhythmic=payload.get("rhythmic", ""),
                tags=payload.get("tags") or [],
                notes=payload.get("notes") or [],
            )
        except ST.Rejected as e:
            return self._fail(400, str(e))
        finally:
            con.close()
        return self._send(201, {"id": r["id"], "title": r["title"],
                                "n_char": r["n_char"]})

    def do_DELETE(self):
        try:
            u = urllib.parse.urlparse(self.path)
            if not u.path.startswith("/api/poem/"):
                return self._fail(404, "未知路径")
            if not self.server.allow_write:
                return self._fail(403, "只读模式，删除需 --allow-write")
            try:
                pid = int(u.path.rstrip("/").rsplit("/", 1)[1])
            except ValueError:
                return self._fail(400, "id 必须是整数")
            con = ST.open_rw(self.server.db, self.server.strains_db)
            try:
                ST.delete_poem(con, pid)
            except ST.Rejected as e:
                return self._fail(403, str(e))
            finally:
                con.close()
            return self._send(200, {"deleted": pid})
        except Exception as e:
            self._fail(500, "%s: %s" % (type(e).__name__, e))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._fail(400, "Content-Length 非法")
            return None
        if n <= 0:
            self._fail(400, "请求体为空")
            return None
        if n > MAX_BODY:
            self._fail(413, "请求体超过 %d 字节" % MAX_BODY)
            return None
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._fail(400, "JSON 解析失败：%s" % e)
            return None
        if not isinstance(obj, dict):
            self._fail(400, "请求体必须是 JSON 对象")
            return None
        return obj


def _pub(r: dict) -> dict:
    """对外输出形态：字典 id 还原成人可读字段，顺带给出朝代/体裁中文名。"""
    return {
        "id": r["id"],
        "title": r.get("title") or "",
        "author": r.get("author") or None,          # 佚名 = null
        "dynasty": r.get("dynasty"),
        "dynasty_label": S.DYNASTY_LABEL.get(r.get("dynasty", ""), ""),
        "kind": r.get("kind"),
        "kind_label": S.KIND_LABEL.get(r.get("kind", ""), ""),
        "rhythmic": r.get("rhythmic") or "",
        "lines": (r.get("body") or "").split("\n"),
        "tags": [t for t in (r.get("tags") or "").split("\x1f") if t],
        "score": r.get("score", 0),
        "n_char": r.get("n_char", 0),
        "src": r.get("src") or "",
        "strains": r.get("strains_text") or None,   # 没挂平仄包就是 null
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(here, "..", "dist", "poetry.db"))
    ap.add_argument("--strains-db", default=os.path.join(here, "..", "dist", "poetry-strains.db"))
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认只听本机。改成 0.0.0.0 会对外暴露，务必先确认网络环境")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow-write", action="store_true", help="开放 POST / DELETE")
    ap.add_argument("--debug", action="store_true", help="打印每条请求日志")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("找不到数据库：%s" % args.db)
    # 启动时只开一条连接做自检（挂载检查、条数），之后每个请求各开各的
    boot = ST.open_ro(args.db, args.strains_db)

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server((args.host, args.port), Handler)
    srv.db = os.path.abspath(args.db)
    srv.strains_db = os.path.abspath(args.strains_db) if os.path.exists(args.strains_db) else ""
    srv.allow_write = args.allow_write
    srv.debug = args.debug

    print("诗词库接口已启动 http://%s:%d" % (args.host, args.port))
    print("  库: %s（%d 条）" % (os.path.abspath(args.db), ST.stats(boot)["n_poems"]))
    print("  平仄包: %s" % ("已挂载" if Q.has_strains(boot) else "未挂载"))
    print("  写接口: %s" % ("开启" if args.allow_write else "关闭（加 --allow-write 开启）"))
    print("  试一下: curl \"http://%s:%d/api/search?q=明月&limit=3\"" % (args.host, args.port))
    boot.close()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
