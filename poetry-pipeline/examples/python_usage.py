# -*- coding: utf-8 -*-
"""
Python 接入示例（可直接运行，覆盖读、写、bundle 三条路径）。

    python examples/python_usage.py

每一步都打印真实结果，方便你确认"我的环境里跑出来的是什么"，
而不是照抄一段没验证过的代码。

依赖: Python 3.11+ / zstandard==0.23.0 / zhconv==1.4.3
"""

from __future__ import annotations
import os, sys, json, glob

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.dirname(HERE)
sys.path.insert(0, PIPE)

import schema as S
import query as Q
import store as ST

DB = os.path.join(PIPE, "dist", "poetry.db")
SDB = os.path.join(PIPE, "dist", "poetry-strains.db")
BUNDLE = os.path.join(PIPE, "dist", "bundle")


def hr(t):
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def main():
    if not os.path.exists(DB):
        sys.exit("找不到 %s，先跑：python build.py --out ./dist" % DB)

    strains = SDB if os.path.exists(SDB) else ""
    con = ST.open_rw(DB, strains)

    # ---------------------------------------------------------------- 1 检索
    hr("1. 全文检索（繁体输入自动转简体）")
    for q in ("乡愁", "鄉愁"):
        rows = Q.search(con, q, limit=3)
        print("  查询 %r -> 命中 %d 条" % (q, len(rows)))
        for r in rows[:2]:
            print("     [%s/%s] %s 《%s》 id=%d"
                  % (S.DYNASTY_LABEL.get(r["dynasty"]), r["kind"],
                     r["author"] or "佚名", r["title"], r["id"]))

    hr("2. 带过滤的检索：唐诗 + 体裁=诗 + 作者=李白")
    for r in Q.search(con, "明月", dynasty="tang", kind="poem",
                      author="李白", limit=5):
        print("   《%s》 %s  id=%d" % (r["title"], r["author"], r["id"]))

    # ---------------------------------------------------------------- 2 作者 / 词牌 / 标题
    hr("3. 按作者 / 词牌 / 标题")
    print("  作者 苏轼：%d 条" % len(Q.by_author(con, "苏轼", 20)))
    print("  词牌 水调歌头：%d 条" % len(Q.by_rhythmic(con, "水调歌头", 20)))
    for r in Q.by_title(con, "静夜思", 3):
        print("  标题《静夜思》-> id=%d 作者=%s" % (r["id"], r["author"]))

    # ---------------------------------------------------------------- 3 详情 + 平仄
    hr("4. 单条详情（含平仄）")
    pid = Q.search(con, "明月几时有", limit=1)[0]["id"] if Q.search(con, "明月几时有", limit=1) \
        else con.execute("SELECT id FROM poems WHERE id BETWEEN ? AND ? LIMIT 1",
                         (5 << 27, (5 << 27) + (1 << 27) - 1)).fetchone()[0]
    d = Q.get_by_id(con, pid)
    dy, kd, seq = S.parse_id(pid)
    print("  id=%d -> 朝代=%s 体裁=%s 序号=%d" % (pid, dy, kd, seq))
    print("  《%s》 %s" % (d["title"], d["author"] or "佚名"))
    print("  正文: " + " / ".join(d["body"].split("\n")[:3]))
    print("  平仄: %s" % (d.get("strains_text") or "（未挂载平仄包，--strains-db 指定）"))

    # ---------------------------------------------------------------- 4 写入
    hr("5. 新增一条并立刻检索到（最后会自动删掉）")
    added = ST.add_poem(con, dynasty="custom", kind="poem",
                        title="接口示例", author="程枢",
                        lines=["示例第一句明月光", "示例第二句地上霜"],
                        tags=["示例"])
    print("  写入 id=%d" % added["id"])
    hit = Q.by_author(con, "程枢", 10)
    print("  按作者查到 %d 条（说明新增立即可检索）" % len(hit))
    try:
        ST.delete_poem(con, 12345)          # 故意删一条构建期数据
    except ST.Rejected as e:
        print("  越权删除被拒绝：%s" % e)
    ST.delete_poem(con, added["id"])
    print("  已清理自建条目，当前自建 %d 条" % ST.count_user_added(con))

    # ---------------------------------------------------------------- 5 bundle
    hr("6. 直接读 bundle 分片（Web / 移动端走的就是这条路径）")
    if not os.path.isdir(BUNDLE):
        print("  bundle 不存在，跳过")
    else:
        import zstandard as zstd
        man = json.load(open(os.path.join(BUNDLE, "manifest.json"), encoding="utf-8"))
        print("  schema=%s  共 %d 条 / %d 分片"
              % (man["schema_version"], man["total_records"], man["total_shards"]))
        sh = sorted(man["shards"], key=lambda s: -s["bytes"])[0]
        print("  最大分片: %s  %d 条  %.2f MB（压缩率 %.2f）"
              % (sh["file"], sh["records"], sh["bytes"] / 1048576,
                 sh["bytes"] / sh["raw_bytes"]))
        raw = zstd.ZstdDecompressor().decompress(
            open(os.path.join(BUNDLE, sh["file"]), "rb").read(),
            max_output_size=64 * 1024 * 1024)
        first = json.loads(raw.decode("utf-8").split("\n", 1)[0])
        print("  首条: i=%d 标题=%s 作者=%s" % (first["i"], first.get("t"), first.get("a")))
        dy, kd, _sq = S.parse_id(first["i"])
        print("  id 反解 -> 朝代=%s 体裁=%s" % (dy, kd))
        print("  字段: " + ", ".join("%s=%s" % (f["k"], f["desc"].split("（")[0])
                                      for f in man["fields"]))

    # ---------------------------------------------------------------- 6 平仄包
    hr("7. 平仄可选包（按 id 关联）")
    if not Q.has_strains(con):
        print("  未挂载。用 build.py --with-strains 生成 poetry-strains.db，")
        print("  再 ST.open_rw(db, strains_db) 挂上即可。")
    else:
        n = con.execute("SELECT COUNT(*) FROM strainsdb.poem_strains").fetchone()[0]
        row = con.execute("SELECT id,data,len FROM strainsdb.poem_strains LIMIT 1").fetchone()
        print("  平仄记录 %d 条" % n)
        print("  样例 id=%d -> %s" % (row[0], S.unpack_strains(row[1], row[2])[:40]))
        # 主包与平仄包靠 id 对齐：取一条有平仄的诗，正文与平仄逐字对照
        p = con.execute("SELECT p.id, p.body FROM strainsdb.poem_strains ps "
                        "JOIN poems p ON p.id=ps.id LIMIT 1").fetchone()
        if p:
            st = Q.strains_of(con, p[0])
            line = p[1].split("\n")[0]
            print("  正文: %s" % line)
            print("  平仄: %s" % st[:len(line)])

    con.close()
    print("\n完成。")


if __name__ == "__main__":
    main()
