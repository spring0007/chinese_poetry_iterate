# -*- coding: utf-8 -*-
"""
查询层。

v2 起库里只有 body 一份正文（简体 + 无空格），不再有 body_s 简体副本；
用户输入的查询词走与构建期同一个 norm_text（转简体 + 去空格 + 跑到不动点），
所以输繁体、输带空格的写法都能命中。作者为佚名时返回 None（对应 author_id=0）。

为什么不用 FTS5（实测依据，254,248 条宋诗）：
    FTS5 trigram   索引使库 59MB -> 245.6MB，且 2 字查询（"明月"/"梅花"）命中 0
    正文 bigram 倒排  15,021,993 行，库 792.4MB，构建 230s —— 否决
    LIKE 全表扫描   命中 1.7~2.5ms，未命中 92ms，零额外空间 —— 采用

用法:
    python query.py --db ./dist/poetry.db search 乡愁
    python query.py --db ./dist/poetry.db --strains-db ./dist/poetry-strains.db get 134217728
    python query.py --db ./dist/poetry.db search 明月 --dynasty tang --limit 5
    python query.py --db ./dist/poetry.db author 李白
    python query.py --db ./dist/poetry.db bench

平仄是可选包：主库里没有 poem_strains 表，要用 --strains-db 挂上
poetry-strains.db 才会显示平仄。没挂也能正常查，只是不显示平仄。
"""

from __future__ import annotations
import sqlite3, sys, os, argparse, time, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


_HERE = os.path.dirname(os.path.abspath(__file__))


def _here_db(name: str) -> str:
    """默认库路径按脚本位置解析，不依赖当前工作目录。

    why：在 PyCharm 里右键运行、或从别的目录 `python .../query.py` 时，
    工作目录不一定是工程根，写死的 "./dist/poetry.db" 会直接变成"找不到数据库"。
    """
    return os.path.join(_HERE, "dist", name)


def to_simplified(q: str) -> str:
    """
    查询词归一到与库里 body 完全相同的形态：简体 + 无空格。
    必须与构建期的 norm_text 是同一个函数——两边各写一套归一规则，
    迟早会出现"库里是 A、查询变成 B"的静默漏召回。
    """
    try:
        return S.norm_text(q)
    except RuntimeError:          # zhconv 缺失时退化到只去空格，并明确告知
        print("  [WARN] 未安装 zhconv，繁体查询词无法转换", file=sys.stderr)
        return "".join(c for c in q if c not in S._SPACES)


def _dynasty_range(dyn: str) -> tuple[int, int]:
    """用 id 高位做朝代过滤，不需要 dynasty 列。"""
    d = S.DYNASTY.get(dyn, 0)
    lo = d << (S.KIND_BITS + S.SEQ_BITS)
    return lo, lo + (1 << (S.KIND_BITS + S.SEQ_BITS)) - 1


STRAINS_SCHEMA = "strainsdb"


def open_db(path: str, strains_db: str = "") -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit("找不到数据库：%s（先跑 build.py）" % path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    # 版本校验只告警不退出：query.py 是只读工具，能查就让它查，但必须把
    # "你手上这个库不是当前版本" 说清楚，否则用户会把版本不匹配导致的结果差异
    # 当成数据本身的问题。
    try:
        row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0].split(".")[0] != S.SCHEMA_VERSION.split(".")[0]:
        print("  [WARN] 库 schema 版本 %s，当前代码 %s —— 建议重新 build.py"
              % (row[0], S.SCHEMA_VERSION), file=sys.stderr)
    if strains_db:
        if not os.path.exists(strains_db):
            print("  [WARN] 平仄库不存在：%s（用 build.py --with-strains 生成）" % strains_db,
                  file=sys.stderr)
        else:
            # ATTACH 而不是新开连接：平仄包与主库靠 id 关联，跨库 JOIN/UNION 都要在同连接内
            con.execute("ATTACH DATABASE ? AS %s" % STRAINS_SCHEMA, (strains_db,))
    return con


def has_strains(con) -> bool:
    """平仄包是否已挂载。查 PRAGMA database_list 而不是直接查 strainsdb.sqlite_master——
    没 ATTACH 时后者会抛 'no such table'，把"可选功能没开"变成"查询报错"。"""
    for _i, name, _f in con.execute("PRAGMA database_list").fetchall():
        if name == STRAINS_SCHEMA:
            return True
    return False


def strains_of(con, pid: int) -> str:
    """取平仄文本。没挂平仄包或该条无平仄时返回空串——不是错误，诗不一定都有平仄。"""
    if not has_strains(con):
        return ""
    row = con.execute("SELECT data,len FROM %s.poem_strains WHERE id=?" % STRAINS_SCHEMA,
                      (pid,)).fetchone()
    return S.unpack_strains(row["data"], row["len"]) if row else ""


COLS = ("p.id AS pid, a.name AS author, p.title AS title, "
        "r.name AS rhythmic, p.body, p.tags, p.notes, p.score, p.n_char, s.name AS src")

_FROM = ("FROM poems p "
         "LEFT JOIN authors a ON a.id = p.author_id "
         "LEFT JOIN rhythmics r ON r.id = p.rhythmic_id "
         "LEFT JOIN sources s ON s.id = p.src_id")


def _decorate(row) -> dict:
    """还原字典 id 为人可读字段；dynasty/kind 从 id 高位反解，不占存储。"""
    d = dict(row)
    d["id"] = d["pid"]
    d["dynasty"], d["kind"], d["_seq"] = S.parse_id(d["pid"])
    d["author"] = d.get("author") or None      # 佚名 -> NULL
    for k in ("title", "rhythmic", "src", "tags", "notes"):
        d[k] = d.get(k) or ""
    return d


def search(con, q: str, dynasty="", kind="", author="", limit=10, offset=0):
    """
    全文子串检索。库已全量简体归一，只查 body 一次；
    查询词先转简体，所以用户输繁体同样能命中。
    """
    sql = "SELECT %s %s WHERE p.body LIKE ?" % (COLS, _FROM)
    args = ["%" + to_simplified(q) + "%"]
    if dynasty:
        lo, hi = _dynasty_range(dynasty)
        sql += " AND p.id BETWEEN ? AND ?"
        args += [lo, hi]
    if kind:
        sql += " AND (p.id >> ?) & 7 = ?"
        args += [S.SEQ_BITS, S.KIND.get(kind, 0)]
    if author:
        sql += " AND a.name = ?"
        args.append(to_simplified(author))
    sql += " ORDER BY p.score DESC, p.id LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [_decorate(r) for r in con.execute(sql, args).fetchall()]


def by_author(con, author: str, limit=20, offset=0):
    sql = "SELECT %s %s WHERE a.name = ? ORDER BY p.score DESC, p.id LIMIT ? OFFSET ?" % (COLS, _FROM)
    return [_decorate(r) for r in con.execute(sql, (to_simplified(author), limit, offset)).fetchall()]


def count_author(con, author: str) -> int:
    return con.execute("SELECT COUNT(*) %s WHERE a.name = ?" % _FROM,
                       (to_simplified(author),)).fetchone()[0]


def by_title(con, title: str, limit=20, offset=0):
    """
    标题检索。v2 起标题直接存主表（不进字典表）——实测 27.3 万唯一标题 /
    34.5 万首诗，重复率仅 1.26 次/值，字典表省不下空间却要多付两份索引。

    why 先等值再子串：ix_poems_title 有 11.5 MB，只有等值 / 前缀能用上，
    LIKE '%x%' 用不上。先走一次等值（毫秒级，命中完整标题时直接返回），
    不够再退化为子串扫描（约 1 s）。这样索引不是白建的，语义也不丢。
    """
    t = to_simplified(title)
    rows = [_decorate(r) for r in con.execute(
        "SELECT %s %s WHERE p.title = ? ORDER BY p.score DESC, p.id LIMIT ? OFFSET ?" % (COLS, _FROM),
        (t, limit, offset)).fetchall()]
    if rows:
        # 等值命中就不再跑子串兜底：实测 LIKE '%x%' 在只有 1 个匹配时要把
        # ix_poems_score 走完一遍（884 ms），而等值已经用 ix_poems_title 拿到了结果。
        return rows
    return [_decorate(r) for r in con.execute(
        "SELECT %s %s WHERE p.title LIKE ? ORDER BY p.score DESC, p.id LIMIT ? OFFSET ?" % (COLS, _FROM),
        ("%" + t + "%", limit, offset)).fetchall()]


def title_count(con, title: str) -> int:
    t = to_simplified(title)
    n = con.execute("SELECT COUNT(*) %s WHERE p.title = ?" % _FROM, (t,)).fetchone()[0]
    if n:
        return n
    return con.execute("SELECT COUNT(*) %s WHERE p.title LIKE ?" % _FROM,
                       ("%" + t + "%",)).fetchone()[0]


def by_rhythmic(con, r: str, limit=20, offset=0):
    """
    词牌检索。先在 rhythmics 字典（1,462 行，name 上有唯一索引）里取 id，
    再用 id 过滤主表——这样 ix_poems_rhythmic 能用上；直接写 r.name = ?
    会让那个索引变成 3.3 MB 的摆设。
    """
    rid = con.execute("SELECT id FROM rhythmics WHERE name = ?", (to_simplified(r),)).fetchone()
    if rid is None:
        return []
    sql = "SELECT %s %s WHERE p.rhythmic_id = ? ORDER BY p.score DESC, p.id LIMIT ? OFFSET ?" % (COLS, _FROM)
    return [_decorate(x) for x in con.execute(sql, (rid[0], limit, offset)).fetchall()]


def count_rhythmic(con, r: str) -> int:
    rid = con.execute("SELECT id FROM rhythmics WHERE name = ?", (to_simplified(r),)).fetchone()
    if rid is None:
        return 0
    return con.execute("SELECT COUNT(*) FROM poems WHERE rhythmic_id = ?", (rid[0],)).fetchone()[0]


def search_count(con, q: str, dynasty="", kind="", author="") -> int:
    """与 search() 完全相同的 WHERE 条件，只取 COUNT——供分页算总页数。"""
    sql = "SELECT COUNT(*) %s WHERE p.body LIKE ?" % _FROM
    args = ["%" + to_simplified(q) + "%"]
    if dynasty:
        lo, hi = _dynasty_range(dynasty)
        sql += " AND p.id BETWEEN ? AND ?"
        args += [lo, hi]
    if kind:
        sql += " AND (p.id >> ?) & 7 = ?"
        args += [S.SEQ_BITS, S.KIND.get(kind, 0)]
    if author:
        sql += " AND a.name = ?"
        args.append(to_simplified(author))
    return con.execute(sql, args).fetchone()[0]


def run_query(con, *, kw="", dynasty="", kind="", author="", rhy="", limit=50, offset=0):
    """
    统一检索入口，返回 (total, rows)。GUI 用它做分页——
    total 是命中的总条数（与 limit/offset 无关），rows 是当前页的数据。

    三种入口与旧 do_search 一致：词牌优先（忽略其它文本过滤）、
    纯作者检索、关键词检索（0 命中时兜底查标题）。
    """
    if rhy:
        rows = by_rhythmic(con, rhy, limit, offset)
        return count_rhythmic(con, rhy), rows
    if author and not kw:
        rows = by_author(con, author, limit, offset)
        return count_author(con, author), rows
    rows = search(con, kw, dynasty, kind, author, limit, offset)
    total = search_count(con, kw, dynasty, kind, author)
    if total == 0 and kw:                       # 0 命中兜底：关键词其实是标题
        rows = by_title(con, kw, limit, offset)
        total = title_count(con, kw)
    return total, rows


def get_by_id(con, pid: int):
    row = con.execute("SELECT %s %s WHERE p.id = ?" % (COLS, _FROM), (pid,)).fetchone()
    if row is None:
        return None
    out = _decorate(row)
    out["strains_text"] = strains_of(con, pid)
    arow = con.execute("SELECT id,dynasty,desc FROM authors WHERE id="
                       "(SELECT author_id FROM poems WHERE id=?)", (pid,)).fetchone()
    if arow:
        out["author_id"] = arow["id"]
        out["author_dynasty"] = arow["dynasty"]
        out["author_desc"] = arow["desc"]
    else:
        out["author_id"] = 0
        out["author_dynasty"] = ""
        out["author_desc"] = ""
    return out


def show(rows, full=False):
    for r in rows:
        body = r["body"].replace("\n", "／")
        if not full and len(body) > 60:
            body = body[:60] + "…"
        meta = "[%s/%s] " % (S.DYNASTY_LABEL.get(r["dynasty"], r["dynasty"]), r["kind"])
        who = r["author"] if r["author"] else "佚名"
        tag = (" #" + ",".join(r["tags"].split("\x1f"))) if r["tags"] else ""
        print("  %s%-14s 《%s》 score=%d id=%d%s" %
              (meta, who, r["title"], r["score"], r["id"], tag))
        print("      " + body)


def bench(con):
    print("=== 检索延迟实测（含首次冷启动）===")
    cases = [("明月", "2字高频"), ("梅花", "2字高频"), ("乡愁", "2字需繁转简"),
             ("秋風", "2字繁体输入"), ("千里共婵娟", "5字名句"), ("不存在的词xyz", "未命中全扫")]
    lat = []
    for q, desc in cases:
        t = time.time()
        rows = search(con, q, limit=20)
        dt = (time.time() - t) * 1000
        lat.append(dt)
        print("  %-12s %-10s 命中=%-4d %7.1f ms" % (q, desc, len(rows), dt))
    print("  P50=%.0f ms  P95=%.0f ms  max=%.0f ms"
          % (statistics.median(lat), sorted(lat)[int(len(lat) * 0.95)], max(lat)))

    print("\n=== 次级索引路径（验证索引真的被用上）===")
    for fn, arg, desc in ((by_title, "静夜思", "标题等值（走 ix_poems_title）"),
                          (by_title, "春", "标题子串（退化全扫）"),
                          (by_rhythmic, "水调歌头", "词牌（走 rhythmics 唯一索引 + ix_poems_rhythmic）"),
                          (by_author, "李白", "作者（走 ix_authors_name + ix_poems_author）")):
        t = time.time()
        rows = fn(con, arg, 20)
        print("  %-46s 命中=%-4d %7.1f ms" % (desc, len(rows), (time.time() - t) * 1000))

    n = con.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
    sz = os.path.getsize(con.execute("PRAGMA database_list").fetchone()[2]) / 1048576
    print("  库: %d 条, %.1f MB" % (n, sz))
    print("  平仄包: %s" % ("已挂载 " + str(con.execute(
        "SELECT COUNT(*) FROM %s.poem_strains" % STRAINS_SCHEMA).fetchone()[0]) + " 条"
        if has_strains(con) else "未挂载（--strains-db 指定 poetry-strains.db）"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_here_db("poetry.db"))
    ap.add_argument("--strains-db", default="", help="平仄可选包 poetry-strains.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search"); p.add_argument("q")
    p.add_argument("--dynasty", default=""); p.add_argument("--kind", default="")
    p.add_argument("--author", default=""); p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("author"); p.add_argument("name"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("title"); p.add_argument("name"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("rhythmic"); p.add_argument("name"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("get"); p.add_argument("id", type=int)
    sub.add_parser("bench")

    args = ap.parse_args()
    con = open_db(args.db, args.strains_db)

    if args.cmd == "search":
        rows = search(con, args.q, args.dynasty, args.kind, args.author, args.limit)
        print("命中 %d 条（limit=%d）" % (len(rows), args.limit))
        show(rows, args.full)
    elif args.cmd == "author":
        show(by_author(con, args.name, args.limit), args.full)
    elif args.cmd == "title":
        show(by_title(con, args.name, args.limit), args.full)
    elif args.cmd == "rhythmic":
        show(by_rhythmic(con, args.name, args.limit), args.full)
    elif args.cmd == "get":
        r = get_by_id(con, args.id)
        if not r:
            print("未找到 id=%d" % args.id); return
        print("id=%s  朝代=%s  体裁=%s  序号=%s" %
              (r["id"], S.DYNASTY_LABEL.get(r["dynasty"], r["dynasty"]), r["kind"], r["_seq"]))
        who = r["author"] if r["author"] else "佚名"
        print("《%s》 %s%s" % (r["title"], who,
                              "  [" + r["rhythmic"] + "]" if r["rhythmic"] else ""))
        for line in r["body"].split("\n"):
            print("   " + line)
        if r.get("strains_text"):
            print("  平仄: " + r["strains_text"][:80])
        if r["tags"]:
            print("  标签: " + ", ".join(r["tags"].split("\x1f")))
        if r.get("author_desc"):
            print("  作者: " + r["author_desc"][:120])
        print("  score=%d 字数=%d  src=%s" % (r["score"], r["n_char"], r["src"]))
    elif args.cmd == "bench":
        bench(con)
    con.close()


if __name__ == "__main__":
    main()
