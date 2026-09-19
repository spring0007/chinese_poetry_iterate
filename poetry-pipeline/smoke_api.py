# -*- coding: utf-8 -*-
"""接口冒烟：真起一个 HTTP 服务，逐个端点打一遍，含并发与写权限校验。"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import subprocess
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")

PY = sys.executable
PORT = 8791
BASE = "http://127.0.0.1:%d" % PORT
out = []


def put(s):
    out.append(s)


def req(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"_raw": raw[:200]}


srv = subprocess.Popen(
    [PY, "-u", os.path.join(HERE, "examples", "api_server.py"),
     "--db", os.path.join(HERE, "dist", "poetry.db"),
     "--strains-db", os.path.join(HERE, "dist", "poetry-strains.db"),
     "--port", str(PORT), "--allow-write"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    encoding="utf-8", errors="replace")

try:
    # 等服务起来
    ok = False
    for _ in range(100):
        try:
            c, j = req("GET", "/api/health")
            if c == 200 and j.get("ok"):
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.2)
    put("health: %s  %s" % (ok, json.dumps(j, ensure_ascii=False) if ok else "启动失败"))
    if not ok:
        raise SystemExit("服务没起来")

    c, j = req("GET", "/api/stats")
    put("stats: %d  %s" % (c, json.dumps(j, ensure_ascii=False)))

    c, j = req("GET", "/api/search?q=%E6%98%8E%E6%9C%88&limit=3")
    put("search 明月: %d  命中 %d 条，首条《%s》%s"
        % (c, j.get("total", 0), j["items"][0]["title"],
           j["items"][0]["author"] or "佚名") if c == 200 else "%d %s" % (c, j))

    # 繁体输入
    c1, j1 = req("GET", "/api/search?q=" + urllib.parse.quote("鄉愁") + "&limit=3")
    c2, j2 = req("GET", "/api/search?q=" + urllib.parse.quote("乡愁") + "&limit=3")
    same = [x["id"] for x in j1.get("items", [])] == [x["id"] for x in j2.get("items", [])]
    put("繁简等价 鄉愁/乡愁: %d/%d 条，结果一致=%s"
        % (j1.get("total", 0), j2.get("total", 0), same))

    pid = j["items"][0]["id"]
    c, j = req("GET", "/api/poem/%d" % pid)
    put("poem/%d: %d 《%s》 平仄=%s"
        % (pid, c, j.get("title"), "有" if j.get("strains") else "null"))

    c, j = req("GET", "/api/authors?q=%E6%9D%8E&limit=3")
    put("authors 李: %d 条 -> %s" % (c, "/".join(x["name"] for x in j.get("items", []))))

    c, j = req("GET", "/api/rhythmics?q=%E6%B0%B4%E8%B0%83&limit=3")
    put("rhythmics 水调: %d 条 -> %s" % (c, "/".join(x["name"] for x in j.get("items", []))))

    # ---- 并发：20 个同时打，验证每请求独立连接真的解决跨线程问题
    errs = []
    lat = []

    def hit(i):
        t = time.time()
        try:
            # 用 f-string 拼：URL 里的 %E6 这类百分号编码会被 % 格式化吃掉
            c, j = req("GET", f"/api/search?q=%E6%98%8E%E6%9C%88&limit=5&offset={i}")
            if c != 200:
                errs.append((i, c, str(j)[:120]))
        except Exception as e:
            errs.append((i, "EXC", "%s: %s" % (type(e).__name__, e)))
        lat.append((time.time() - t) * 1000)

    ts = [threading.Thread(target=hit, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lat.sort()
    put("并发 20 请求: 失败 %d 个，P50=%.0f ms P95=%.0f ms"
        % (len(errs), lat[len(lat) // 2], lat[int(len(lat) * 0.95)]))
    for e in errs[:3]:
        put("  失败样例: %s" % (e,))
    put("[PASS] 并发请求无跨线程错误" if not errs else "[FAIL] 并发下报错")

    # ---- 写接口
    c, j = req("POST", "/api/poem", {
        "dynasty": "custom", "kind": "poem", "title": "接口冒烟条目",
        "author": "接口测试", "lines": ["牀前明月光", "疑是地上霜"], "tags": ["冒烟"]})
    put("POST /api/poem: %d  %s" % (c, json.dumps(j, ensure_ascii=False)))
    new_id = j.get("id")

    c, j = req("GET", "/api/mine?limit=5")
    put("GET /api/mine: %d 条 -> %s"
        % (len(j.get("items", [])),
           "/".join("%s《%s》" % (x["author"] or "佚名", x["title"]) for x in j["items"])))

    # 删除自建条目
    c, j = req("DELETE", "/api/poem/%d" % new_id)
    put("DELETE 自建 %d: %d %s" % (new_id, c, json.dumps(j, ensure_ascii=False)))

    # 删除构建期数据应被拒
    c, j = req("DELETE", "/api/poem/%d" % pid)
    put("DELETE 构建期 %d: %d %s" % (pid, c, json.dumps(j, ensure_ascii=False)))
    put("[PASS] 构建期数据拒绝删除" if c == 403 else "[FAIL] 构建期数据被删了")

    # 参数边界
    c, j = req("GET", "/api/search")
    put("缺参数: %d %s" % (c, json.dumps(j, ensure_ascii=False)))
    c, j = req("GET", "/api/search?q=%E6%98%8E%E6%9C%88&limit=999999")
    put("limit 越界被夹住: %d 返回 %d 条" % (c, len(j.get("items", []))))
    c, j = req("GET", "/api/poem/abc")
    put("非法 id: %d %s" % (c, j.get("error")))
    c, j = req("GET", "/api/nope")
    put("未知路径: %d %s" % (c, j.get("error")))

    put("SMOKE_OK")
except Exception:
    import traceback
    put("SMOKE_FAIL")
    put(traceback.format_exc())
finally:
    srv.terminate()
    try:
        tail = srv.communicate(timeout=10)[0]
    except Exception:
        tail = ""
    srv.kill()
    put("--- 服务端输出 ---")
    put((tail or "").strip()[:1500])

print("\n".join(out))
sys.exit(0 if "SMOKE_OK" in out else 1)
