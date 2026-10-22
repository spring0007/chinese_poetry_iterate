# -*- coding: utf-8 -*-
"""生成简化版测试库：每个「朝代 × 类型」抽 N 首，供 App 功能测试使用。

# 为什么需要它
完整库 35.7 万首 / ~350 MB，塞进手机或 CI 都太重；而 App 的功能测试（列表滚动、
检索、详情页、译文/赏析/配图面板、平仄对照）根本用不到全量数据——它需要的是
"每种体裁、每个朝代都有几条，而且**带扩展资料**"，不是"数据齐全"。

# 抽取口径
- 按 (dynasty, kind) 分组，每组最多 --per-group 首（默认 100）。
- 组内**先取带扩展资料的**（poem_extras 里有行），再用固定 seed 随机补足。
  why 不直接取 id 最小的 100 条：那会让每组几乎全是同一本书、同一个作者的连续条目，
  UI 上一眼就能看出是造出来的数据；而优先取带资料的，是为了让译文 / 赏析 / 配图
  这几个面板在测试库上真的有内容可渲染（否则一打开全是空面板，等于没测）。
- 另外**强制保留全局 score 最高的 --top-score 首**（默认 100），不论落在哪组。
  why 要单列这条：上面那步是"每组都有"，抽签性质，高分榜头部全是唐诗/宋诗，
  随机抽样很可能整榜缺席 —— 于是"按热度排序 / 热门推荐"这类 UI 在测试库上没法验。
  这 N 首是**额外叠加**在分组配额之上的（若已被某组抽中就只去重，不占额外名额）。
- 固定 seed 保证**可复现**：同一条命令跑两次得到一模一样的库，"第 37 条显示不对"
  这种描述才有意义，否则每次重建数据都换一批，问题无法复现。高分榜有 id 兜底排序，
  同样可复现（score 并列时按 id 升序取，不会因 SQLite 内部顺序不同而漂移）。

# 为什么沿用主库的 id / uid，不重新编号
主库的平仄包 poetry-strains.db 是按 **id** 关联的。子集化之后 id 保持原值，
所以简化版可以直接 `ATTACH` 现成的平仄包，不必再单独出一份简化平仄包；
uid 同理（extras.jsonl 里的记录也是按 uid 挂的，取出来的子集照样能对回去）。

# 产物
一份**独立完整**的库：poems / authors / rhythmics / sources / genre / poem_extras / meta
都只含被抽中的那部分，索引与主库同构，sqlite3.connect 打开就能用。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S

HERE = os.path.dirname(os.path.abspath(__file__))

# 从主库原样复制结构的表。顺序不重要（表间没有外键约束，id 也只是普通整数）。
TABLES = ("poems", "authors", "rhythmics", "genre", "sources",
          "poem_extras", "meta")

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


def pick_ids(src: str, per_group: int, seed: int, top_n: int = 100):
    """挑出要保留的 poems.id，返回 (id 列表, {组名: 实际条数}, 高分榜统计)。

    why 在 Python 里分组、而不是写一段窗口函数 SQL：这里要的是"带资料的优先 +
    组内随机洗牌 + 报告每组实际取到多少条"三段逻辑，SQL 能写，但会变成一段没人
    愿意维护的嵌套子查询；而这点数据量（几十万行）在内存里过一遍只要几秒。
    """
    top_n = max(0, top_n)
    con = sqlite3.connect(src)
    try:
        has_ex = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='poem_extras'"
        ).fetchone()[0] > 0
        rich_uids = set()
        if has_ex:
            rich_uids = {r[0] for r in con.execute("SELECT uid FROM poem_extras")}
        buckets: dict = {}
        for pid, uid in con.execute("SELECT id, uid FROM poems"):
            key = (pid >> (S.KIND_BITS + S.SEQ_BITS), (pid >> S.SEQ_BITS) & 0x7)
            buckets.setdefault(key, []).append((pid, uid in rich_uids))
        # 全局高分榜：score 最高的 top_n 首，不论落在哪组都保进来。
        # why 走 WHERE score>0：score 是 heat_score.py 产出的 0-255 量化热度，0 表示
        # 没有热度数据（诗经/近现代等未评估的集合全是 0），把它们算进"高分榜"
        # 只会用 score=0 的条目填满尾部，失去意义——库小到不够 top_n 条时就少取。
        # id 兜底排序：score 并列（100 分就有成百上千首）时按 id 升序，保证两次构建
        # 抽到的是同一批，可复现性不被 SQLite 的内部行序左右。
        top = [r[0] for r in con.execute(
            "SELECT id FROM poems WHERE score > 0 ORDER BY score DESC, id ASC LIMIT ?",
            (top_n,))] if top_n else []
    finally:
        con.close()

    rnd = random.Random(seed)
    picked, detail = [], {}
    for key in sorted(buckets):
        items = buckets[key]
        rich = [p for p, has in items if has]
        plain = [p for p, has in items if not has]
        # 两堆各自洗牌：保证"先取带资料的"这个优先级不被随机性冲掉。
        rnd.shuffle(rich)
        rnd.shuffle(plain)
        got = (rich + plain) if per_group <= 0 else (rich + plain)[:per_group]
        got.sort()                      # 回主库顺序，插库时页写入更顺
        picked.extend(got)
        dyn, kind = S.DYNASTY_NAME.get(key[0], "?%d" % key[0]), S.KIND_NAME.get(key[1], "?%d" % key[1])
        detail["%s/%s" % (dyn, kind)] = len(got)

    # 高分榜叠加到分组配额之上：已被某组抽中的只去重、不占额外的"每组几条"。
    picked_set = set(picked)
    added = [i for i in top if i not in picked_set]
    if added:
        picked.extend(added)
        picked.sort()                   # 同样回主库顺序
    return picked, detail, {"n": top_n, "kept": len(top), "added": len(added)}


def build_sample(src: str, out: str, per_group: int = 100, seed: int = 42,
                 top_n: int = 100) -> dict:
    ids, detail, top = pick_ids(src, per_group, seed, top_n)
    if not ids:
        raise SystemExit("没抽到任何诗——源库可能是空的：%s" % src)

    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    con.execute("PRAGMA page_size=8192")     # 与主库一致：正文普遍 > 4KB，页小反而多溢出页
    con.execute("ATTACH DATABASE ? AS src", (os.path.abspath(src),))
    try:
        # 1) 结构照抄主库（含列定义与默认值），保证"简化版是主库的真子集"这件事
        #    连 schema 层面都成立——下游代码只写一套。
        for name in TABLES:
            row = con.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?",
                (name,)).fetchone()
            if not row or not row[0]:
                raise SystemExit("源库缺少表 %s —— 它可能不是完整的 poetry.db" % name)
            con.execute(row[0])

        # 2) 诗：把选中的 id 灌进一张临时表再 JOIN，而不是拼一个 978 个 `?` 的
        #    IN 列表——SQLite 的变量上限随版本而变（老版本只有 999），拼列表等于
        #    把"抽多少首"和"sqlite 版本"绑在一起，迟早会在某个机器上炸。
        con.execute("CREATE TEMP TABLE _sel(id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO _sel VALUES(?)", [(i,) for i in ids])
        con.execute("INSERT INTO poems SELECT * FROM src.poems "
                    "WHERE id IN (SELECT id FROM _sel)")

        # 3) 被引用的字典行。只带用得上的：简化版的目的就是"小"，
        #    留着一堆一条诗都不指向的作者/词牌只会让人误以为数据不完整。
        #    作者按 author_uid 取（v3.4 起主表只有这一列）：authors.uid 上有唯一索引，
        #    这条 IN 子查询走索引而不会退化成 978 行 × 全表扫。
        con.execute("INSERT INTO authors SELECT * FROM src.authors "
                    "WHERE uid IN (SELECT DISTINCT author_uid FROM poems "
                    "WHERE author_uid<>'')")
        con.execute("INSERT INTO rhythmics SELECT * FROM src.rhythmics "
                    "WHERE id IN (SELECT DISTINCT rhythmic_id FROM poems WHERE rhythmic_id<>0)")
        con.execute("INSERT INTO sources SELECT * FROM src.sources "
                    "WHERE id IN (SELECT DISTINCT src_id FROM poems WHERE src_id<>0)")
        # 体裁字典是全局约定（id 1..8 跨库同一套），子集里往往只剩两三种，但字典要
        # 整份带上：否则「诗=1」在精简库里查不到名称，客户端还要另配一张表。
        # （v3.7 删掉了 source 朝代表——朝代在 id 高位里，子集照抄 id 即可反解。）
        con.execute("INSERT INTO genre SELECT * FROM src.genre")
        # 3b) n_poems 按子集重算。它抄自主库（口径是"指向本行的作品数"），
        #     但子集里每位作者名下只剩几首，照抄会让 UI 显示"111 首"而实际只有 20 条。
        #     它是派生数据，重算不影响"简化版是主库真子集"——诗、uid、id 一个没动。
        con.execute("UPDATE authors SET n_poems="
                    "(SELECT COUNT(*) FROM poems p WHERE p.author_uid = authors.uid)")
        # 4) 扩展资料。孤儿行（uid 在主库 poems 里找不到）不带——测试库不需要它们，
        #    留着还会让 `verify_db.py` 的孤儿率断言看起来莫名其妙。
        con.execute("INSERT INTO poem_extras SELECT * FROM src.poem_extras "
                    "WHERE uid IN (SELECT uid FROM poems)")

        # 5) meta 先整份抄过来（schema_version / note / text_form 等都要跟着走），
        #    再把与"总量"有关的键按子集重算。
        con.execute("INSERT INTO meta SELECT * FROM src.meta")

        def count(t):
            return con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]

        stats = {"poems": count("poems"), "authors": count("authors"),
                 "rhythmics": count("rhythmics"), "sources": count("sources"),
                 "genre": count("genre"), "extras": count("poem_extras")}
        parent = con.execute("SELECT v FROM src.meta WHERE k='n_poems'").fetchone()
        con.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", [
            ("n_poems", str(stats["poems"])),
            ("n_authors", str(stats["authors"])),
            ("n_rhythmics", str(stats["rhythmics"])),
            # 这几条是样本标记：让拿到库的人一眼看出它不是全量库，
            # 也知道它是从哪份主库、用什么口径抽出来的（可复现）。
            ("sample", "1"),
            ("sample_parent", os.path.basename(src)),
            ("sample_parent_rows", parent[0] if parent else "?"),
            ("sample_per_group", str(per_group)),
            ("sample_seed", str(seed)),
            # 高分榜口径也要落库：它同样影响"库里有哪些诗"，只写 per_group/seed
            # 不足以复现这份库。
            ("sample_top_score", str(top["n"])),
            ("sample_top_added", str(top["added"])),
            ("sample_groups", json.dumps(detail, ensure_ascii=False, sort_keys=True)),
            ("sample_built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
            # 简化库不建 FTS5：几百到几千行上用 LIKE 已经足够快，而 FTS 的体积放大
            # 是 4 倍以上——为了"小"才做的这份库，没理由再背一块。
            ("fts", "0"),
        ])

        # 6) 索引照抄（放在灌完数据之后建，批量插入时少维护一轮索引）。
        for (sql,) in con.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall():
            con.execute(sql)
        con.execute("ANALYZE")
        con.commit()
    finally:
        con.execute("DETACH DATABASE src")
        con.close()

    # 7) 压实。ATTACH 期间不能 VACUUM，所以放到 detach 之后再单独开一次连接。
    con = sqlite3.connect(out)
    con.isolation_level = None
    con.execute("PRAGMA page_size=8192")     # VACUUM 按当前连接的 page_size 重建整库
    con.execute("VACUUM")
    size = os.path.getsize(out)
    con.close()
    return {"stats": stats, "detail": detail, "size": size, "out": out,
            "ids": len(ids), "top": top}


def main():
    ap = argparse.ArgumentParser(
        description="生成简化版测试库：每个「朝代 × 类型」抽 N 首，并保底收全局高分榜")
    ap.add_argument("--db", default=os.path.join(HERE, "dist", "poetry.db"),
                    help="源主库，默认 ./dist/poetry.db")
    ap.add_argument("--out", default=os.path.join(HERE, "dist", "poetry-sample.db"),
                    help="输出库，默认 ./dist/poetry-sample.db")
    ap.add_argument("--per-group", type=int, default=100,
                    help="每个 (朝代,类型) 最多抽几首；0 = 不限（默认 100）")
    ap.add_argument("--top-score", type=int, default=100,
                    help="额外强制保留全局 score 最高的几首；0 = 不要（默认 100）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，固定值保证可复现")
    ap.add_argument("--dry-run", action="store_true", help="只报告抽取口径，不写文件")
    args = ap.parse_args()

    t0 = time.time()
    if args.dry_run:
        ids, detail, top = pick_ids(args.db, args.per_group, args.seed, args.top_score)
        for k, v in sorted(detail.items()):
            print("  %-14s %4d 首" % (k, v))
        print("  %-14s %4d 首（其中 %d 首是组外新增）"
              % ("[score 高分榜]", top["kept"], top["added"]))
        print("共 %d 组 / %d 首（未写文件）" % (len(detail), len(ids)))
        return

    print("源库 %s" % args.db)
    r = build_sample(args.db, args.out, args.per_group, args.seed, args.top_score)
    print("\n抽取结果（%d 组 / %d 首）：" % (len(r["detail"]), r["ids"]))
    for k, v in sorted(r["detail"].items()):
        print("  %-14s %4d 首" % (k, v))
    t = r["top"]
    print("  %-14s %4d 首（其中 %d 首是组外新增）"
          % ("[score 高分榜]", t["kept"], t["added"]))
    s = r["stats"]
    print("\n产物 %s" % r["out"])
    print("  %.1f MB   poems %d / authors %d / rhythmics %d / sources %d / "
          "genre %d / poem_extras %d"
          % (r["size"] / 1048576, s["poems"], s["authors"], s["rhythmics"],
             s["sources"], s["genre"], s["extras"]))
    print("  平仄：id 沿用主库原值，可直接 ATTACH 现成的 poetry-strains.db")
    print("\n总耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
