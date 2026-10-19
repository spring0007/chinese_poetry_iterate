# -*- coding: utf-8 -*-
"""extras 落库层：把 extras/ 下的 JSONL 分片镜像成主库里的 poem_extras 表。

# 为什么 JSONL 之外还要在库里存一份
extras.py 那套 sidecar（JSONL + images/）解决的是**采集与累积**的问题：流式追加、
按行 seek、单条覆写、可直接挂 CDN，且天然免疫 build.py 的整库重建。
但消费侧要的是另一件事：**和诗词一起查**。"给我这首诗的全部资料"（正文 + 作者 +
译文 + 注释 + 赏析 + 配图）在库内是一次 JOIN，走 sidecar 则要求调用方先理解
JSONL 的偏移索引、再自己拼装。"所有 19xx 年之后、带赏析、来自 A 来源的诗" 更是
只有 SQL 才写得出来。

所以这里的分工是：**JSONL 是权威源，poem_extras 是由它重放出来的只读镜像**。
重建主库时重新导入一次，两边永远一致；也正是因此本表允许被整表 DELETE 后重灌。
写路径仍然只走 extras.py（ExtrasStore.put / remove_field），不要在库里直接改
poem_extras —— 那样改出来的内容下次同步就没了。

# 为什么用 uid（而不是 poems.id）做唯一 id
poems.id 的低 24 位是**序号**，去重或集合顺序一变就整体位移；uid 只由内容派生，
重建不变。extras 是"挂在诗外面"的数据，必须用对重建免疫的键来关联，否则每重建
一次译文/赏析就集体错位（schema.make_uid 的注释里记了完整的实测数据）。

# 注释（notes）为什么在这里
源语料自带的注释原本落在主库 poems.notes 列（`\x1f` 拼接），与 extras 的 nt 是
同一类内容却分居两处，查"这首诗有没有注释"要问两个地方。v3.2 把它的内容迁进了
poem_extras.notes；**v3.3 干脆把 poems.notes 这一列删掉**——注释的唯一存放处就是
本表，构建期由 build.py 把源语料里的注释直接交过来，不再"先落主库、再搬一次"
（那条往返链路正是 v3.2 丢过一次数据的原因）。

**注释内容必须先写回权威源 extras.jsonl**（export_notes），再重放成表。
why：poem_extras 是"重放出来的镜像"，镜像里出现源文件没有的数据，下一次 sync
就会把它整片抹掉——v3.2 实测过：只写表不写源的话，重放一次 notes 从 3,687 掉回
3,020、648 条 source='poem-notes' 的行全消失，而当时主库那一列已清空、无从恢复。
只有落回 JSONL，"重放"才是可复现、可幂等的。v3.3 之后主库连"退路"都没有了，
这条约束更要守死。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extras as X

# 表名沿用主库 poem_fts / 平仄包 poem_strains 的命名习惯：poem_ 前缀 + 用途。
TABLE = "poem_extras"

# 列顺序与 extras.DB_COLUMNS 的取值顺序一致，便于对照阅读。
COLUMNS = tuple(X.DB_COLUMNS.values())
INSERT_SQL = "INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (
    TABLE, ",".join(COLUMNS), ",".join("?" * len(COLUMNS)))

DDL = """CREATE TABLE IF NOT EXISTS %s(
    uid                 TEXT PRIMARY KEY NOT NULL,
    translation         TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    introduction        TEXT NOT NULL DEFAULT '',
    creative_background TEXT NOT NULL DEFAULT '',
    appreciation        TEXT NOT NULL DEFAULT '',
    images              TEXT NOT NULL DEFAULT '',
    updated             INTEGER NOT NULL DEFAULT 0
)""" % TABLE

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


class Rejected(Exception):
    """输入不合法。带明确原因，直接显示给用户。"""


def default_root() -> str:
    """默认 extras 目录按脚本位置解析，不依赖当前工作目录。

    why 不在 dist/ 下：dist/ 整个被 .gitignore 忽略，而 extras 是采集成果、
    重建不出来，放那儿等于没有版本保护（2026-10-17 清 dist 时差点永久丢失）。
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "extras")


def has_table(con, table: str = TABLE) -> bool:
    """表是否存在。读路径（query.py）靠它判断能不能 JOIN——旧库（schema < 3.2）
    没有这张表，直接 JOIN 会报 no such table，而不是"没有扩展资料"。"""
    return con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()[0] > 0


def create_table(con) -> None:
    con.execute(DDL)
    # 不为任何列建额外索引：本表唯一的访问方式是按 uid 点查，主键已覆盖；
    # 数组列约定用 \x1f 连接（见 extras.DB_SEP），本质是"一列里塞了个小列表"，
    # 建索引没有意义。v3.2 那个 ix_poem_extras_source 随 source 列一起去掉了。


def _read_jsonl(path: str):
    """逐行产出记录。坏行跳过而不中断——一行坏了不该让整表同步不了，
    但要计数上报，否则脏数据会静默消失。"""
    bad = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                bad += 1
    if bad:
        print("  [WARN] %s 有 %d 行不是合法 JSON，已跳过" % (os.path.basename(path), bad))


def _read_all_jsonl(parts: list):
    """按分片顺序产出全部记录（why 分片见 extras.JSONL_PART_MAX 的说明）。"""
    for p in parts:
        for rec in _read_jsonl(p):
            yield rec


def read_notes_from_db(db_path: str) -> dict:
    """从（旧）库的 `poems.notes` 列读出 {uid: 注释文本}，供迁移期使用。

    why 单独成函数：v3.3 起主库已经没有 notes 列，这个函数的唯一用途就是把
    3.2 及更早版本的库里那一列捞出来写回 JSONL。列不存在时返回空字典而不是报错
    ——对 3.3 的库跑它应当安静地什么都不做。
    """
    out = {}
    if not db_path:
        return out
    if not os.path.exists(db_path):
        raise Rejected("旧库不存在：%s" % db_path)
    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(poems)")]
        if "notes" not in cols:
            return out
        for uid, notes in con.execute("SELECT uid, notes FROM poems WHERE notes<>''"):
            if not uid:
                continue
            txt = (notes or "").replace(X.DB_SEP, "\n").strip()
            if txt:
                out[uid] = txt
    finally:
        con.close()
    return out


def export_notes(root: str = None, notes_map: dict = None, from_db: str = None,
                 enabled: bool = True, source: str = "poem-notes",
                 lic: str = "user") -> dict:
    """
    把注释写回**权威源** extras.jsonl（作为 `nt`）。

    注释从哪来，两种入口（都给时以 notes_map 为准）：
      - notes_map —— {uid: 文本}，build.py 从源语料直接收集后传进来（常规路径）；
      - from_db   —— 旧库路径，从中读 `poems.notes`（迁移期一次性工具）。
    两个都不给 = 无事可做，直接返回空统计。

    why 必须写 JSONL、而不是直接写 poem_extras：poem_extras 是 JSONL 重放出来的
    镜像。镜像里一旦出现源文件没有的数据，下一次重放就把它整片抹掉——注释迁移
    正好是这种情况（实测：只写镜像的话，重放一次 notes 3,687 → 3,020，
    648 条来源标记为 poem-notes 的行全没，而当时主库那列已清空、无从恢复）。
    落回 JSONL 之后，"重放"才可复现、可幂等。

    幂等：这段文本已经出现在 JSONL 的 `nt` 里就不再追加；重复跑第二遍 written=0。
    重叠：两边都有注释时**不覆盖**，把新来的那段按行拼在后面（原有内容在前）——
    "丢数据比重复更不可接受"，这是迁移一开始就定下的口径。
    署名：新建的行记 src=poem-notes；已有别的来源的行只把 poem-notes 追加进
    asrc，并且**不覆盖**它原有的 lic——那是别的来源的许可，不能被顶掉。
    """
    out = {"enabled": bool(enabled), "source_rows": 0, "written": 0,
           "new_rows": 0, "merged_rows": 0, "skipped": 0, "jsonl_rows": 0}
    if not enabled:
        return out
    if notes_map is None and from_db:
        notes_map = read_notes_from_db(from_db)
    if not notes_map:
        return out
    store = X.ExtrasStore(root)
    store.load()
    out["source_rows"] = len(notes_map)
    for uid, notes in notes_map.items():
        txt = (notes or "").replace(X.DB_SEP, "\n").strip()
        if not txt:
            continue
        rec = store.get(uid) or {}
        cur = (rec.get("nt") or "").strip()
        if txt in cur:
            out["skipped"] += 1         # 权威源已经收着这段文本了（含重复跑本函数）
            continue
        is_new = not rec
        payload = (cur + "\n" + txt) if cur else txt
        store.put(uid, {"nt": payload}, source=source,
                  lic=(lic if is_new else ""))
        out["written"] += 1
        out["new_rows" if is_new else "merged_rows"] += 1
    if out["written"]:
        out["jsonl_rows"] = store.flush()
        store.write_manifest()
    else:
        out["jsonl_rows"] = len(store)
    return out


def sync(con, root: str = None, notes_map: dict = None, from_db: str = None,
         batch: int = 2000) -> dict:
    """用 extras.jsonl 全量重放 poem_extras（并可选把注释写回权威源）。

    返回统计字典。整表先清空再灌入：JSONL 是唯一权威，增量合并只会让两边
    慢慢漂移——真要增量，请改 JSONL 后重新跑本函数。

    notes_map / from_db 是注释的来源（见 export_notes），要在重放**之前**先落回
    JSONL：顺序不能反，否则"只有这批语料里有注释"的那些诗在重放时还没进源文件，
    会被整个漏掉。两个都不传 = 纯重放，不改 JSONL。
    """
    root = os.path.abspath(root or default_root())
    parts = X.jsonl_parts(root)
    if not parts:
        raise Rejected("extras 数据不存在：%s 下既没有 %s 也没有 %s 分片"
                       "（该目录是纯累积数据，重建主库不会生成它，需先跑采集脚本）"
                       % (root, X.JSONL, X.JSONL_PART_GLOB))

    create_table(con)
    # meta 可能在（正常构建）；也可能不在（有人拿本函数给裸库补表），兜一手。
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")

    # 1) 注释先落回权威源，再重放——否则这批数据进了镜像却不在源里，
    #    下一次 sync 就会把它整片抹掉（见 export_notes 的说明）。
    exp = export_notes(root, notes_map=notes_map, from_db=from_db)

    # 2) 整表重放。注释此刻已在 JSONL 里，走的是同一条 to_db_row 路径，
    #    不再需要"仅语料里有注释"的特殊补行分支。
    con.execute("DELETE FROM %s" % TABLE)
    n_jsonl = 0
    buf = []

    def flush():
        if buf:
            con.executemany(INSERT_SQL, buf)
            buf.clear()

    for rec in _read_all_jsonl(parts):
        row = X.to_db_row(rec)
        if not row["uid"]:
            continue                      # 没有 uid 的记录无法与主库关联，丢弃并计入
        n_jsonl += 1
        buf.append([row[c] for c in COLUMNS])
        if len(buf) >= batch:
            flush()
    flush()

    n_rows = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    n_orphan = con.execute(
        "SELECT COUNT(*) FROM %s e LEFT JOIN poems p ON p.uid=e.uid "
        "WHERE p.uid IS NULL" % TABLE).fetchone()[0]

    meta_rows = [
        ("extras_table", TABLE),
        ("extras_rows", str(n_rows)),
        ("extras_source", ",".join(os.path.basename(p) for p in parts)),
        ("extras_synced_at", time.strftime(X.TS_FMT)),
        ("extras_orphans", str(n_orphan)),
    ]
    # notes_migrated = "有多少条注释已经归到 extras 体系里"（这次新写进 JSONL 的
    # + 早就已经在 JSONL 里的）。只在真的读到过注释时才改写：源语料里的注释是
    # 一次性写进来的，之后每次重放读到的都是 0，无脑覆盖会把"历史上迁过 734 条"
    # 这个事实抹掉，而它正是排查"注释为什么住在 poem_extras 里"的第一手线索。
    n_migrated = exp["written"] + exp["skipped"]
    if n_migrated:
        meta_rows.append(("notes_migrated", str(n_migrated)))
    con.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", meta_rows)
    con.commit()

    return {"rows": n_rows, "from_jsonl": n_jsonl,
            "notes_written": exp["written"],
            "notes_merged": exp["merged_rows"],
            "notes_skipped": exp["skipped"],
            "orphans": n_orphan, "root": root}


def import_notes(old_db: str, root: str = None) -> dict:
    """把旧库（schema ≤ 3.2）`poems.notes` 那一列的内容写回权威源 extras.jsonl。

    **迁移期一次性工具**：v3.3 起主库已没有 notes 列，常规构建由 build.py 直接把
    源语料里的注释交给 sync（notes_map 参数），不经过任何表。本函数只在"手上只有
    一份老库、想把里面的注释捞出来"时用。只动 JSONL、不动任何表——重放交给 sync。
    """
    return export_notes(root, from_db=old_db)


def compact(con, page_size: int = 8192) -> int:
    """灌完大文本后压实文件（VACUUM），返回压实后的字节数。

    why 必须显式设 page_size：VACUUM 是按**当前连接的** page_size 重建整库的，
    而新连接默认 4096 —— 不设就等于把 emit_sqlite 特意选的 8192 悄悄改回去，
    正文普遍超过 4KB，页小反而多出一截溢出页。实测（357,186 首 + 97,230 行
    extras）：不压实 347.1 MB，压实后见构建日志。

    why 不写在 sync() 里面：调用方可能连着跑多步插入，每次都 VACUUM 会把构建
    时间白拖长；压实是收尾动作，由调用方决定什么时候付这个时间。
    """
    con.commit()
    con.isolation_level = None          # VACUUM 不能在事务里执行
    con.execute("PRAGMA page_size=%d" % page_size)
    con.execute("VACUUM")
    n_page = con.execute("PRAGMA page_count").fetchone()[0]
    real_page = con.execute("PRAGMA page_size").fetchone()[0]
    return n_page * real_page


def stats(con) -> dict:
    """各字段的填充条数 + 配图引用数 + 更新时间范围。

    不再统计"来源分布"：source 列在 v3.3 已从本表移除（溯源信息留在权威源
    extras.jsonl 里）。要看来源分布请跑 `extras.py stats`——那个读的是 JSONL。
    """
    if not has_table(con):
        return {"table": TABLE, "exists": False}
    out = {"table": TABLE, "exists": True,
           "rows": con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]}
    for col in COLUMNS:
        if col == "uid":
            continue
        if col == "updated":
            # 整数列用"非 0"表示有值——空串那套语义只对文本列成立
            out["updated"] = con.execute(
                "SELECT COUNT(*) FROM %s WHERE updated>0" % TABLE).fetchone()[0]
            lo, hi = con.execute(
                "SELECT MIN(updated),MAX(updated) FROM %s" % TABLE).fetchone()
            out["updated_min"], out["updated_max"] = lo or 0, hi or 0
            continue
        out[col] = con.execute(
            "SELECT COUNT(*) FROM %s WHERE %s<>''" % (TABLE, col)).fetchone()[0]
    out["orphans"] = con.execute(
        "SELECT COUNT(*) FROM %s e LEFT JOIN poems p ON p.uid=e.uid "
        "WHERE p.uid IS NULL" % TABLE).fetchone()[0]
    # 图片是 \x1f 连接的地址列表，引用总数要拆开数（不是"有图的记录数"）
    n_ref = 0
    for (v,) in con.execute("SELECT images FROM %s WHERE images<>''" % TABLE):
        n_ref += len([x for x in v.split(X.DB_SEP) if x])
    out["image_refs"] = n_ref
    return out


def check(con) -> list:
    """断言式自检，返回 [(ok, 名称, 详情)]。供 verify_db.py 与 CLI 共用。"""
    res = []
    if not has_table(con):
        return [(False, "poem_extras 表存在", "缺少该表（schema < 3.2？）")]

    n = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    n_bad_uid = con.execute(
        "SELECT COUNT(*) FROM %s WHERE uid=''" % TABLE).fetchone()[0]
    res.append((n_bad_uid == 0, "poem_extras.uid 全部非空", "%d 行空 uid" % n_bad_uid))

    # uid 是主键，重复在写库时就会 IntegrityError，这里再确认一次防"表被手改过"
    n_dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT uid FROM %s GROUP BY uid HAVING COUNT(*)>1)"
        % TABLE).fetchone()[0]
    res.append((n_dup == 0, "poem_extras.uid 唯一", "%d 组重复" % n_dup))

    n_orphan = con.execute(
        "SELECT COUNT(*) FROM %s e LEFT JOIN poems p ON p.uid=e.uid "
        "WHERE p.uid IS NULL" % TABLE).fetchone()[0]
    # 孤儿允许存在（外部数据先到、诗后被删），但必须是已知数量，不能是意外
    res.append((n_orphan * 100 <= max(n, 1), "poem_extras 孤儿行 < 1%",
                "%d / %d 行在 poems.uid 里找不到（%.2f%%）"
                % (n_orphan, n, 100.0 * n_orphan / max(n, 1))))

    # v3.3：注释的唯一存放处是 poem_extras.notes，主库那一列必须已经不存在。
    # why 断言"列没了"而不是"列是空的"：两列并存才是真正危险的状态——写一侧漏一处
    # 就会让同一个字段有两个家，读侧还得靠 COALESCE 猜谁更有权威。
    has_notes_col = any(r[1] == "notes" for r in con.execute("PRAGMA table_info(poems)"))
    res.append((not has_notes_col, "poems 表没有 notes 列（注释只在 poem_extras）",
                "该列仍然存在" if has_notes_col else "已移除"))

    # 数组列必须是 \x1f 连接，不能混进 JSON 文本——两种表示并存下游就得猜
    n_json = con.execute(
        "SELECT COUNT(*) FROM %s WHERE images LIKE '[%%'" % TABLE).fetchone()[0]
    res.append((n_json == 0, "数组列用 \\x1f 连接（非 JSON 文本）", "%d 行像 JSON" % n_json))

    # 配图列只存**纯文件名**（如 395f….jpg），不带 images/ 之类的目录前缀。
    # why 断言而不是只靠约定：一旦混进带目录的写法，消费方就得先判断前缀，
    # 还得为 `../` 之类的穿越式引用兜底——这条一 FAIL 就该回去跑
    # `extras.py normalize-images --apply`。
    n_dir = con.execute(
        "SELECT COUNT(*) FROM %s WHERE images LIKE '%%/%%' OR images LIKE '%%\\%%'"
        % TABLE).fetchone()[0]
    res.append((n_dir == 0, "配图列只存纯文件名（不含目录前缀）", "%d 行带目录" % n_dir))

    # updated 必须是整数秒。SQLite 是动态类型，列声明成 INTEGER 也拦不住有人把
    # '2026-09-29T16:31:26' 这类文本塞进来——那会让 `WHERE updated > ?` 静默失效
    # （文本与整数比较时类型序不同，结果既不是真也不是假，而是"排到最后"）。
    n_ts = con.execute(
        "SELECT COUNT(*) FROM %s WHERE updated<>0 AND typeof(updated)<>'integer'"
        % TABLE).fetchone()[0]
    res.append((n_ts == 0, "updated 是 Unix 整数秒", "%d 行不是整数" % n_ts))
    return res


def main():
    import argparse
    ap = argparse.ArgumentParser(description="extras 落库：JSONL -> 主库 poem_extras 表")
    ap.add_argument("--db", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dist", "poetry.db"))
    ap.add_argument("--root", default=None, help="extras 目录，默认 ./extras")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="用 extras.jsonl 重放整表")
    p.add_argument("--vacuum", action="store_true",
                   help="灌完压实文件（反复 sync 才需要；约 25 秒，一次性构建没必要）")
    p.add_argument("--notes-from-db", default=None,
                   help="重放前先把这份旧库的 poems.notes 写回 JSONL（迁移期用）")
    e = sub.add_parser("import-notes",
                       help="把 --from-db 这个旧库的 poems.notes 写回 extras.jsonl（不动表）")
    e.add_argument("--from-db", required=True, help="含 poems.notes 列的旧库路径")
    sub.add_parser("stats", help="查看统计")
    sub.add_parser("check", help="断言式自检")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    try:
        if args.cmd == "sync":
            r = sync(con, args.root, from_db=args.notes_from_db)
            print("poem_extras 表 %d 行（JSONL %d 条）" % (r["rows"], r["from_jsonl"]))
            if r["notes_written"] or r["notes_skipped"]:
                print("  注释写回 JSONL %d 条（并入已有 %d、跳过 %d）"
                      % (r["notes_written"], r["notes_merged"], r["notes_skipped"]))
            print("  孤儿行 %d 条" % r["orphans"])
            if args.vacuum:
                print("  压实后 %.1f MB" % (compact(con) / 1048576))
        elif args.cmd == "import-notes":
            r = import_notes(args.from_db, args.root)
            print("旧库 %s：新写入 JSONL %d 条（新建 %d / 并入 %d），"
                  "已有 %d 条（跳过），JSONL 现有 %d 行"
                  % (os.path.basename(args.from_db), r["written"], r["new_rows"],
                     r["merged_rows"], r["skipped"], r["jsonl_rows"]))
        elif args.cmd == "stats":
            print(json.dumps(stats(con), ensure_ascii=False, indent=2))
        elif args.cmd == "check":
            ok = True
            for good, name, detail in check(con):
                ok = ok and good
                print("[%s] %-40s %s" % ("PASS" if good else "FAIL", name, detail))
            print("总体: " + ("全部通过" if ok else "存在失败项"))
            sys.exit(0 if ok else 1)
    except Rejected as e:
        print("拒绝：%s" % e, file=sys.stderr)
        sys.exit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
