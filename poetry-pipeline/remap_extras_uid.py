# -*- coding: utf-8 -*-
"""
remap_extras_uid.py —— 把 dist/extras/extras.jsonl 里的**整型 poem_id** 换成
主库 poems.uid（内容派生的稳定 uuid，形如 4a3c83d8-a6f2-46f7-852b-4d2162d1c6c4）。

为什么非换不可
--------------
poems.id 是 (dynasty << 27) | (kind << 24) | seq 的 int32，**低位 seq 是序号**。
只要 dedup 丢/留一条、或集合顺序一变，seq 就整体位移，id 跟着全变。
extras（译文/注释/赏析/创作背景/配图）原本按这个 id 挂在主库外面，
一重建就全部错位 —— 这正是要根治的问题。

uid 由 (朝代, 作者, 标题, 正文) 做 uuid5 确定性哈希，重建 N 次结果一致，
所以「重建 / 重排序 / 增删别的诗」都不会让它变，适合做外部引用的主键。
（代价见 schema.make_uid 的注释：改这一首自身的内容，uid 会变。）

用法
----
  python remap_extras_uid.py --dry-run     # 演练：只报数，不落盘
  python remap_extras_uid.py               # 真改（自动备份 extras.jsonl.bak-<时间戳>）
  python remap_extras_uid.py --verify      # 只校验：现库里 uid 的命中率

顺序很重要
----------
必须用**当前**这份 db 来做映射 —— extras 里的数字 id 是跟着当前 db 发的号。
换完 uid 之后再重建 db，新库的 uid 由同样的内容算出，自然对得上。
反过来（先重建再映射）数字 id 已经漂了，映射就废了。
"""
from __future__ import annotations
import os, sys, json, sqlite3, time, shutil, argparse, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from schema import parse_id, make_uid          # noqa: E402
from extras import _short                       # noqa: E402  复用同一套序列化，保证格式一致

DEFAULT_DB = os.path.join(HERE, "dist", "poetry.db")
DEFAULT_EXTRAS = os.path.join(HERE, "dist", "extras", "extras.jsonl")


def build_uid_map(db_path: str) -> dict:
    """从当前 db 读出 (数字 id -> uid)。uid 的算法必须与 build.py 完全一致。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    authors = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM authors")}
    m = {}
    for r in con.execute("SELECT id, author_id, title, body FROM poems"):
        dynasty, _kind, _seq = parse_id(r["id"])
        m[r["id"]] = make_uid(dynasty, authors.get(r["author_id"], ""), r["title"], r["body"])
    con.close()
    return m


def looks_like_uid(v) -> bool:
    return isinstance(v, str) and len(v) == 36 and v.count("-") == 4


def build_content_index(db_path: str) -> tuple:
    """建 (标题+正文) / 标题 / 正文 三级索引 → uid，用于「按内容找回一首诗」。

    why 需要它：uid 由内容派生，改了作者（太宗皇帝→李世民）uid 就变，
    extras 里存的旧 uid 会失效。此时只能退回按内容匹配，把它重新指到新 uid。
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    by_tb, by_t, by_b, uid_set = {}, {}, {}, set()
    for r in con.execute("SELECT uid,title,body FROM poems"):
        uid_set.add(r["uid"])
        by_tb.setdefault((r["title"], r["body"]), r["uid"])
        by_t.setdefault(r["title"], r["uid"])
        by_b.setdefault(r["body"], r["uid"])
    con.close()
    return uid_set, by_tb, by_t, by_b


def repair(args) -> int:
    """把「内容被改写 → uid 失效」的 extras 记录，按内容重新指向新诗的 uid。

    依赖两份东西：改之前的 extras 备份（里面有数字 id）和改之前的 db 备份
    （数字 id → 内容）。两者配合才能把一条失效记录还原成"它原本指哪首诗"。
    """
    uid_set, by_tb, by_t, by_b = build_content_index(args.db)

    con = sqlite3.connect(args.old_db)
    con.row_factory = sqlite3.Row
    auth = {r["id"]: r["name"] for r in con.execute("SELECT id,name FROM authors")}
    old_by_id = {}
    for r in con.execute("SELECT id,author_id,title,body FROM poems"):
        old_by_id[r["id"]] = (r["title"], r["body"], auth.get(r["author_id"], ""))
    con.close()

    cur = [l.strip() for l in open(args.extras, "r", encoding="utf-8") if l.strip()]
    bak = [l.strip() for l in open(args.old_extras, "r", encoding="utf-8") if l.strip()]
    if len(cur) != len(bak):
        sys.exit("当前 extras(%d) 与备份(%d) 行数不一致，无法按行对齐，终止。"
                 % (len(cur), len(bak)))

    fixed = unfixable = 0
    how = collections.Counter()
    out = []
    for line, old_line in zip(cur, bak):
        rec = json.loads(line)
        uid = rec.get("i")
        if uid in uid_set:
            out.append(_short(rec))
            continue
        num = json.loads(old_line).get("i")
        info = old_by_id.get(num) if isinstance(num, int) else None
        new_uid = None
        if info:
            title, body, _a = info
            if (title, body) in by_tb:
                new_uid, _h = by_tb[(title, body)], "标题+正文"
            elif title in by_t:
                new_uid, _h = by_t[title], "仅标题"
            elif body in by_b:
                new_uid, _h = by_b[body], "仅正文"
            else:
                _h = ""
            if new_uid:
                how[_h] += 1
        if new_uid:
            rec["i"] = new_uid
            fixed += 1
        else:
            unfixable += 1
        out.append(_short(rec))

    print("\n=== 修复统计 ===")
    print("  失效记录总数          : %d" % (fixed + unfixable))
    print("  按内容重新指回新 uid  : %d" % fixed)
    for k, v in how.most_common():
        print("      └ 匹配依据 %-8s: %d" % (k, v))
    print("  仍然找不到对应诗      : %d  （原样保留，未丢弃）" % unfixable)

    if args.dry_run:
        print("\n[dry-run] 未落盘。")
        return 0
    bak2 = args.extras + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.extras, bak2)
    tmp = args.extras + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in out:
            f.write(s + "\n")
    os.replace(tmp, args.extras)
    print("\n已写入 %s\n备份   %s" % (args.extras, bak2))
    return 0 if unfixable == 0 else 1


def _merge(a: dict, b: dict) -> dict:
    """合并两条落在同一 uid 上的记录：非空值优先，空位由后者补；配图取并集。

    来源不互相顶掉：主来源保留第一条的，后来的记进 asrc —— CC BY 类协议要求
    署名，把 A 家的内容标成 B 家是实打实的合规问题。
    """
    out = dict(a)
    for k, v in b.items():
        if k == "i":
            continue
        if k == "im":
            merged = list(out.get("im") or [])
            for x in (v or []):
                if x not in merged:
                    merged.append(x)
            if merged:
                out["im"] = merged
            continue
        if v in (None, "", [], {}):
            continue
        if not out.get(k):
            out[k] = v
    sb = b.get("src")
    if sb and sb != out.get("src"):
        alts = list(out.get("asrc") or [])
        if sb not in alts:
            alts.append(sb)
            out["asrc"] = alts
    return out


def dedupe(args) -> int:
    """把同一 uid 上重复的多条记录合并成一条。

    为什么会有重复：--repair 用「仅标题 / 仅正文」兜底匹配时，可能把两条本来
    不同的记录指到同一个 uid 上。它们内容并不相同（来源、字段各异），
    而 JSONL 索引是 {uid: offset} —— 同 uid 只有最后一条读得到，
    前面那条就永久够不着了。所以必须合并，不能任其覆盖。
    """
    recs, order = {}, []
    for line in open(args.extras, "r", encoding="utf-8"):
        s = line.strip()
        if not s:
            continue
        r = json.loads(s)
        k = r.get("i")
        if k not in recs:
            recs[k] = r
            order.append(k)
        else:
            recs[k] = _merge(recs[k], r)

    total_lines = sum(1 for l in open(args.extras, "r", encoding="utf-8") if l.strip())
    print("\n=== 去重统计 ===")
    print("  合并前行数 : %d" % total_lines)
    print("  合并后条数 : %d" % len(recs))
    print("  被合并掉   : %d 条（内容已并入同 uid 的记录，未丢弃）" % (total_lines - len(recs)))

    if args.dry_run:
        print("\n[dry-run] 未落盘。")
        return 0
    bak = args.extras + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.extras, bak)
    tmp = args.extras + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k in order:
            f.write(_short(recs[k]) + "\n")
    os.replace(tmp, args.extras)
    print("\n已写入 %s\n备份   %s" % (args.extras, bak))
    return 0


def main():
    ap = argparse.ArgumentParser(description="extras.jsonl 的数字 poem_id → 稳定 uid")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--extras", default=DEFAULT_EXTRAS)
    ap.add_argument("--dry-run", action="store_true", help="只报数不落盘")
    ap.add_argument("--verify", action="store_true", help="只校验命中率，不改文件")
    ap.add_argument("--repair", action="store_true",
                    help="按内容把失效的 uid 重新指回新诗（改过作者/标题后用它）")
    ap.add_argument("--dedupe", action="store_true",
                    help="合并落在同一 uid 上的重复记录（--repair 之后跑）")
    ap.add_argument("--old-db", default="", help="--repair 用：改之前的 db 备份")
    ap.add_argument("--old-extras", default="", help="--repair 用：改之前的 extras 备份")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("找不到主库：%s" % args.db)
    if not os.path.exists(args.extras):
        sys.exit("找不到 extras：%s" % args.extras)

    if args.repair:
        import glob
        if not args.old_db:
            c = sorted(glob.glob(args.db + ".bak-*"))
            if not c:
                sys.exit("找不到 db 备份，请用 --old-db 指定")
            args.old_db = c[-1]
        if not args.old_extras:
            c = sorted(glob.glob(args.extras + ".bak-*"))
            if not c:
                sys.exit("找不到 extras 备份，请用 --old-extras 指定")
            args.old_extras = c[-1]
        print("新库     : %s" % args.db)
        print("旧库备份 : %s" % args.old_db)
        print("旧extras : %s" % args.old_extras)
        return repair(args)

    if args.dedupe:
        return dedupe(args)

    print("读取主库 %s ..." % args.db)
    t0 = time.time()
    uid_map = build_uid_map(args.db)
    print("  诗 %d 条，uid 映射建立完毕，用时 %.1fs" % (len(uid_map), time.time() - t0))
    uid_set = set(uid_map.values())

    # ---------- 只校验 ----------
    if args.verify:
        hit = miss = already = 0
        for line in open(args.extras, "r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            i = json.loads(line).get("i")
            if looks_like_uid(i):
                already += 1
                hit += 1 if i in uid_set else 0
                miss += 0 if i in uid_set else 1
            else:
                miss += 1
        tot = hit + miss
        print("\n=== 校验：uid 在库里的命中情况 ===")
        print("  已是 uid 且能命中 : %d" % hit)
        print("  未命中（仍是数字id，或 uid 对不上）: %d" % miss)
        print("  命中率: %.1f%%" % (100.0 * hit / max(1, tot)))
        return 0 if miss == 0 else 1

    # ---------- 重映射 ----------
    total = remapped = skipped = missing = 0
    out_lines = []
    for line in open(args.extras, "r", encoding="utf-8"):
        s = line.strip()
        if not s:
            continue
        total += 1
        rec = json.loads(s)
        i = rec.get("i")
        if looks_like_uid(i):
            skipped += 1                      # 已经是 uid，幂等，跳过
        elif isinstance(i, int) and i in uid_map:
            rec["i"] = uid_map[i]
            remapped += 1
        else:
            missing += 1                      # 数字 id 在库里找不到，原样保留待人工
        out_lines.append(_short(rec))

    print("\n=== 重映射统计 ===")
    print("  extras 总条数        : %d" % total)
    print("  本次换成 uid         : %d" % remapped)
    print("  已是 uid（跳过）     : %d" % skipped)
    print("  库里找不到对应诗     : %d  （原样保留，未丢弃）" % missing)

    # 冲突检查：两个不同的数字 id 算出同一个 uid（内容完全相同才会发生）
    rev = collections.Counter(uid_map.values())
    dup = sum(1 for v in rev.values() if v > 1)
    print("  库内 uid 冲突组数    : %d" % dup)

    if args.dry_run:
        print("\n[dry-run] 未落盘。去掉 --dry-run 才会真正改写。")
        return 0

    bak = args.extras + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.extras, bak)
    tmp = args.extras + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in out_lines:
            f.write(s + "\n")
    os.replace(tmp, args.extras)
    print("\n已写入 %s" % args.extras)
    print("备份   %s" % bak)
    return 0


if __name__ == "__main__":
    sys.exit(main())
