#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
正文被改写后，把挂不上的 extras 重新指回新 uid。

## 为什么需要这个

`schema.make_uid` 是内容哈希（朝代+作者+标题+正文）。它对「别人增删/重排序」
免疫，但**对自己被改写不免疫**：正文一改，uid 就变。

2026-09-30 用百度百科补齐了 9,025 首被截断的正文 ⇒ 这 9,025 首的 uid 全变了
⇒ 挂在这些 uid 上的 extras（译文/注释/赏析/配图，来自 chagushici 与 LLM）
集体失效。重建后的校验实测：96,582 条里 **4,163 条（4.3%）命中不到主库**。

## 为什么不直接用 remap_extras_uid.py --repair

`--repair` 依赖「改之前的 extras 备份 + 改之前的 db 备份」按行对齐，
而这里 extras 已经比备份多了 690 条（中间跑过 LLM 补录），行数对不上直接终止；
而且那份 db 备份（9/29 15:51）还是 uid 迁移**之前**的，压根没有 uid 列。

## 思路

uid 是哈希，反推不出来，但可以**正向重算**：

1. 写回前 `crosscheck.patch_file` 备份过每一个被改的文件
   （`xcheck/patch-backup/<时间戳>/<相对路径>`）⇒ 里面有**旧正文**。
2. 新库里有这首诗的朝代/作者/标题（这些都没被改）。
3. `make_uid(朝代, 作者, 标题, 旧正文)` 算出来的就是**旧 uid**。
4. 拿旧 uid 去孤儿集合里查：命中就是它（哈希撞号概率可忽略，命中即确认）。

同一个文件可能有好几份备份（试点那批、全量那批、reclean 那批），
全部试一遍，命中即止 —— 不去猜哪份是"改之前的"。
"""

import argparse
import collections
import glob
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sources as SRC                                   # noqa: E402
import schema as S
from schema import make_uid                             # noqa: E402

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xcheck")
BAK_GLOB = os.path.join(WORK, "patch-backup", "*", "*")


def resolve_path(coll, base: str) -> str:
    """
    把集合里的文件名还原成绝对路径。

    why 自己实现而不用 crosscheck._resolve_path：crosscheck.py 已随
    chagushici 参照站退役被移除，这里只需要「按 pattern 找文件」这点能力，
    不值得为它把整个模块拖回来。
    """
    for p in glob.glob(os.path.join(SRC.ROOT, coll.pattern.replace("\\", "/"))):
        if os.path.basename(p) == base:
            return p
    return ""


def norm(s: str) -> str:
    """归一化比较：去掉空白和标点差异。"""
    return "".join(ch for ch in (s or "") if not ch.isspace())


def load_db(db_path: str) -> tuple:
    """(uid 集合, (标题,作者) -> [(uid, dynasty, author, title, body)], 标题 -> 同上)"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT p.uid AS uid, p.title AS title, p.body AS body, "
        "COALESCE(a.name,'') AS author, COALESCE(a.dynasty,'') AS dynasty "
        "FROM poems p LEFT JOIN authors a ON a.id = p.author_id"
    ).fetchall()
    con.close()
    uid_set = set()
    by_ta = collections.defaultdict(list)
    by_t = collections.defaultdict(list)
    for r in rows:
        uid_set.add(r["uid"])
        item = (r["uid"], r["dynasty"], r["author"], r["title"], r["body"] or "")
        by_ta[(r["title"], r["author"])].append(item)
        by_t[r["title"]].append(item)
    return uid_set, by_ta, by_t


def load_backup_index() -> dict:
    """相对路径(带 __) -> [备份文件绝对路径, ...]，按批次时间升序。"""
    idx = collections.defaultdict(list)
    for p in glob.glob(BAK_GLOB):
        if not os.path.isfile(p):
            continue
        idx[os.path.basename(p)].append(p)
    for v in idx.values():
        v.sort()
    return idx


def load_report(csv_path: str) -> list:
    import csv
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows
            if (r.get("判定") or "").startswith(("源被截断", "异文"))]


def main() -> int:
    ap = argparse.ArgumentParser(description="正文改写后重新挂接 extras 的 uid")
    ap.add_argument("--db", default=os.path.join("dist", "poetry.db"))
    ap.add_argument("--extras", default=os.path.join("dist", "extras", "extras.jsonl"))
    ap.add_argument("--csv", default=os.path.join(WORK, "baike-20260930-143849.csv"),
                    help="baike_crosscheck 的报告（提供 集合/文件/序号/原标题）")
    ap.add_argument("--apply", action="store_true", help="写盘；默认只演练")
    args = ap.parse_args()

    uid_set, by_ta, by_t = load_db(args.db)
    print("新库 uid %d 个" % len(uid_set))

    # ---- 1. 找出孤儿（uid 在新库里查不到的 extras 记录）----
    lines = [l.strip() for l in open(args.extras, encoding="utf-8") if l.strip()]
    orphans = set()
    for l in lines:
        try:
            i = json.loads(l).get("i")
        except Exception:
            continue
        if i and i not in uid_set:
            orphans.add(i)
    print("extras %d 条，其中挂不上 %d 条（%.1f%%）"
          % (len(lines), len(orphans), len(orphans) * 100.0 / max(len(lines), 1)))

    # ---- 2. 反算旧 uid ----
    bak_idx = load_backup_index()
    rows = load_report(args.csv)
    colls = {c.name: c for c in SRC.COLLECTIONS}
    cache = {}
    mapping = {}
    how = collections.Counter()
    stat = collections.Counter()

    def body_at(path: str, idx: int, field: str) -> str:
        """
        取备份文件里这一首的旧正文。

        why 要 join：源库的正文字段是**列表**（一句一行），而 db 里的 body 是
        `\\n`.join 后的字符串。make_uid 用的是后者，所以这里也必须拼起来，
        否则类型不对还是小事，算出来的 uid 会完全不同。
        """
        key = (path, idx)
        if key in cache:
            return cache[key]
        try:
            data = json.loads(open(path, encoding="utf-8-sig").read())
            if isinstance(data, dict):
                data = [data]
            v = data[idx].get(field) or ""
            if isinstance(v, list):
                # why 必须过一遍 norm_lines：make_uid 用的是**构建归一之后**的正文
                # （build.normalize_all 会转简体、去空格），不是源 JSON 里的原文。
                # 少了这一步，凡是正文里有繁体字或空格的诗，反算出的 uid 全都不对。
                v = "\n".join(S.norm_lines([str(x) for x in v]))
        except Exception:
            v = ""
        cache[key] = v
        return v

    for r in rows:
        coll = colls.get(r.get("集合", ""))
        if not coll:
            stat["集合不存在"] += 1
            continue
        path = resolve_path(coll, r.get("文件", ""))
        if not path:
            stat["找不到文件"] += 1
            continue
        try:
            rel = os.path.relpath(path, SRC.ROOT).replace(os.sep, "__").replace("/", "__")
        except ValueError:
            rel = os.path.basename(path)
        try:
            idx = int(r.get("文件内序号"))
        except (TypeError, ValueError):
            stat["序号缺失"] += 1
            continue

        # 这首诗在新库里的候选（标题+作者，退化到仅标题）
        cands = by_ta.get((r.get("原标题", ""), r.get("源作者", "")), [])
        if not cands:
            cands = by_t.get(r.get("原标题", ""), [])
        if not cands:
            stat["新库里找不到这首诗"] += 1
            continue

        # why 两轮候选：报告里的「源作者」没做过归一（可能还是庙号、带朝代前缀），
        # 直接拿它去 db 里对作者会落空。所以先用 (标题, 作者) 精确候选，
        # 没命中再退化到「仅标题」—— uid 是哈希，命中即确认，放宽候选不会误配。
        cand_sets = [("标题+作者", cands)]
        if cands is not by_t.get(r.get("原标题", ""), []):
            cand_sets.append(("仅标题", by_t.get(r.get("原标题", ""), [])))

        hit = False
        for label, group in cand_sets:
            for bak in bak_idx.get(rel, []):
                old_body = body_at(bak, idx, coll.body)
                if not old_body:
                    continue
                for uid, dyn, author, title, _body in group:
                    old_uid = make_uid(dyn, author, title, old_body)
                    if old_uid in orphans and old_uid not in mapping:
                        mapping[old_uid] = uid
                        how[label] += 1
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        stat["命中" if hit else "未命中"] += 1

    print("\n=== 反算结果 ===")
    print("  报告里的改写条目      : %d" % len(rows))
    print("  反算出旧 uid 并命中   : %d" % len(mapping))
    for k, v in how.most_common():
        print("      └ %-16s: %d" % (k, v))
    for k, v in stat.most_common():
        print("  %-22s: %d" % (k, v))
    print("  仍然孤儿              : %d" % (len(orphans) - len(mapping)))

    if not args.apply:
        print("\n[演练] 未写盘。加 --apply 落盘。")
        return 0
    if not mapping:
        print("\n没有可修复的映射，不动文件。")
        return 0

    bak = args.extras + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.extras, bak)
    out = []
    fixed = 0
    for l in lines:
        try:
            rec = json.loads(l)
        except Exception:
            out.append(l)
            continue
        if rec.get("i") in mapping:
            rec["i"] = mapping[rec["i"]]
            fixed += 1
        out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    tmp = args.extras + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, args.extras)
    print("\n已改写 %d 条 extras，写入 %s\n备份 %s" % (fixed, args.extras, bak))
    return 0


if __name__ == "__main__":
    sys.exit(main())
