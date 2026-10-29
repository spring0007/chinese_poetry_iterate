#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诗人分级分类系统（poet tiers）：五档分类与档位分数。

输入（用户给定的五档分类与分数，2026-10-17 更正版）
--------------------------------------------------
    第一档（100 分）：屈原、李白、杜甫、苏轼
    第二档（ 98 分）：陶渊明、曹植、白居易、李商隐、辛弃疾、李清照、毛泽东
    第三档（ 96 分）：王维、杜牧、陆游、李煜、马致远
    第四档（ 94 分）：艾青、穆旦、胡适、北岛、海子、顾城、舒婷、戴望舒、卞之琳
    第五档（ 92 分）：王勃、陈子昂、孟浩然、王昌龄、刘禹锡、郭沫若、闻一多、徐志摩、何其芳

规则
----
1. **档位与分数一一对应**：第 k 档诗人得分为 TIER_SCORES[k]
   （100 / 98 / 96 / 94 / 92），同档诗人同分并列。
2. **分数带**：按 score_band 的更正后标准（第一档 = 90-100 分），
   34 位诗人的分值 92-100 全部落在第 1 分数带内——即全部属于顶级诗人区。
3. **并列名次**：标准竞赛排名（competition ranking，「1224」）：
   同分并列同一名次，其后名次跳到并列人数之后（1,1,1,1, 5,5,5,5,5,5,5,
   12,...）。并列内部按 姓名升序 -> id 升序，输出确定、可复现。
4. **与热度分的关系**：档位分是人工分级口径，覆盖作者的热度聚合分；
   每位诗人的原始热度分与名次留痕在 JSON 的 heat_score / heat_rank 字段。

历史版本
--------
- v1.0（已废弃）：把第五档提升为第一档并内分 5 级，分数按排名槽位取
  分数带上沿（100/98/96/94/92/90/88/86/84）——该标准已声明为错误，
  不再沿用；v1.0 产物可从 git 历史或备份恢复。
- v2.0（当前）：按上述更正后的五档分类与分数重建。

数据库现状
----------
34 位中 22 位在 authors 表有作者行（取其挂诗主行，与 build.py 的
author_uids 口径一致；李煜/刘禹锡同名多行只更新有作品的主行），
12 位（第四档全部 + 闻一多/徐志摩/何其芳）库中无作者行，
只进 JSON（id=null），不建行、不改库。

同步（--apply）
--------------
    rank/poet-tiers.json           权威档位文件（含每位诗人的原始热度分留痕）
    dist/poetry.db                 authors.score = 纯热度聚合分 + 档位覆盖（先备份）
    rank/author-heat-scores.json   纯热度榜 + 22 位诗人的档位分，名次重算（先备份）
    build.py / author_heat.py      已接入档位覆盖层，重建或重跑不会丢档位分

安全闸
------
--apply 先把「非分级作者的当前分值」与「从 poems 重新聚合的纯热度分」
逐条比对，任何漂移都中止（不覆盖可疑数据）；通过后才整库重置并写档位分。

用法
----
    python poet_tiers.py --check      # 只校验分级数据自洽性
    python poet_tiers.py --dry-run    # 打印分级/分数/统计，不写任何文件
    python poet_tiers.py --export     # 只写 rank/poet-tiers.json
    python poet_tiers.py --apply      # 导出 + 备份 + 同步 DB 与热度 JSON + 全量校验
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

from score_band import TOP_TIER_MIN, score_to_tier

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # E:/chinese_poetry_iterate
DB = os.path.join(HERE, "dist", "poetry.db")
RANK_DIR = os.path.join(ROOT, "rank")
OUT = os.path.join(RANK_DIR, "poet-tiers.json")
HEAT_JSON = os.path.join(RANK_DIR, "author-heat-scores.json")

RUBRIC_VERSION = "2.0"

# 历代诗人分级 rubric 写入过的分数集合（v1.0 槽位分 100/98/96/94/92/90/88/86/84
# 与 v2.0 档位分 100/98/96/94/92 的并集）。安全闸用它区分两种「非分级作者
# 当前值 != 纯热度」的情况：属这个集合的是旧 rubric 残留（名单收缩时应被
# 重置），不属这个集合的只能是外部手动改动（必须中止，不能静默覆盖）。
# 将来若 v3.0 启用新分值，把这个集合补全。
LEGACY_TIER_SCORES = {100, 98, 96, 94, 92, 90, 88, 86, 84}


class TierError(Exception):
    """诗人分级系统的输入/数据异常（评分计算、同步前置校验失败）。"""


# ============================================================ 1. 分级数据
# 用户给定的五档分类与分数（更正后）：档位 -> (诗人元组, 档位分数)
TIERS = {
    1: (("屈原", "李白", "杜甫", "苏轼"), 100),
    2: (("陶渊明", "曹植", "白居易", "李商隐", "辛弃疾", "李清照", "毛泽东"), 98),
    3: (("王维", "杜牧", "陆游", "李煜", "马致远"), 96),
    4: (("艾青", "穆旦", "胡适", "北岛", "海子", "顾城", "舒婷",
         "戴望舒", "卞之琳"), 94),
    5: (("王勃", "陈子昂", "孟浩然", "王昌龄", "刘禹锡",
         "郭沫若", "闻一多", "徐志摩", "何其芳"), 92),
}

TIER_POETS = {t: names for t, (names, _s) in TIERS.items()}
TIER_SCORES = {t: score for t, (_n, score) in TIERS.items()}
TIER_LABELS = {1: "第一档", 2: "第二档", 3: "第三档", 4: "第四档", 5: "第五档"}


# ============================================================ 2. 查询接口
def classify(name: str):
    """诗人名 -> 分类 dict；未收录返回 None。

    dict 字段：tier / score / label / score_band（score_band 分数带号，
    按 score_band 规则全部为 1，即 90-100 顶级带）。
    """
    for t, names in TIER_POETS.items():
        if name in names:
            score = TIER_SCORES[t]
            return {
                "tier": t, "score": score, "label": TIER_LABELS[t],
                "score_band": score_to_tier(score),
            }
    return None


def _require(name: str) -> dict:
    c = classify(name)
    if c is None:
        raise TierError("诗人 %r 不在分级系统中（共收录 %d 位）"
                        % (name, sum(len(n) for n in TIER_POETS.values())))
    return c


def get_tier(name: str) -> int:
    """档位（1-5）。"""
    return _require(name)["tier"]


def get_score(name: str) -> int:
    """档位对应的作者分（100/98/96/94/92）。"""
    return _require(name)["score"]


def iter_poets():
    """按排名顺序（档位 1->5，档内姓名升序）遍历全部诗人。"""
    for t in sorted(TIER_POETS):
        for name in sorted(TIER_POETS[t]):
            yield name, classify(name)


def score_overrides() -> dict:
    """{诗人名: 档位分数}——给 build.py / author_heat.py 的覆盖层用。"""
    return {name: c["score"] for name, c in iter_poets()}


def apply_overrides(scores: dict, name_uid: dict) -> int:
    """把档位分数覆盖到 {uid: score} 映射上（build.py 重建路径用，原地修改）。

    name_uid 两种形态都支持：
      - {(name, dynasty): uid}：build.py 重建路径（同名跨朝代各有独立作者行，
        每个朝代的同名作者行都要覆盖为同一档位分——陶渊明 在 魏晋/晋 都算第二档）。
      - {name: uid}：旧调用方 / 单测兼容。
    库中无作者行的诗人自动跳过。返回覆盖的 uid 条数。
    """
    n = 0
    for name, score in score_overrides().items():
        # 先按 (name, dynasty) 键匹配所有同名朝代的作者行
        hit = 0
        for k, uid in name_uid.items():
            kn = k[0] if isinstance(k, tuple) else k
            if kn == name and uid:
                scores[uid] = score
                n += 1
                hit += 1
        # 旧 {name: uid} 形态：没有 (name,dynasty) 键时，按 name 直接查
        if hit == 0 and isinstance(name_uid, dict) and name in name_uid:
            uid = name_uid[name]
            if uid:
                scores[uid] = score
                n += 1
    return n


def apply_overrides_rich(rich: list, con) -> tuple:
    """把档位分数应用到 author_heat 的 rich 记录上（读库聚合路径用）。

    - 已在榜（有热度分诗词）的分级诗人：score 覆盖为档位分；
    - 未上榜但库中有作者行的分级诗人（屈原等无热度分诗词的名家）：
      补一条 n=0 记录，让他们按档位分进榜；
    - 库中无作者行的 12 位：跳过（只在 rank/poet-tiers.json）。
    返回 (rich, 覆盖数, 新增数)；rich 原地修改。
    """
    uid_map = name_uid_map(con)
    by_uid = {r["id"]: r for r in rich}
    n_ovr = n_add = 0
    for name, score in score_overrides().items():
        uid = uid_map.get(name)
        if not uid:
            continue
        r = by_uid.get(uid)
        if r is not None:
            if r["score"] != score:
                r["score"] = score
                n_ovr += 1
            continue
        row = con.execute("SELECT dynasty FROM authors WHERE uid=?", (uid,)).fetchone()
        rec = {"id": uid, "name": name, "dynasty": row[0] if row else "",
               "score": score, "n": 0}
        rich.append(rec)
        by_uid[uid] = rec
        n_add += 1
    return rich, n_ovr, n_add


# ============================================================ 3. 自校验
def validate() -> int:
    """校验分级数据自洽性，返回诗人总数；任何问题抛 TierError。"""
    seen = {}
    for t, names in TIER_POETS.items():
        if not names:
            raise TierError("第%d档没有诗人" % t)
        for name in names:
            if name in seen:
                raise TierError("诗人 %r 重复出现在第%d档与第%d档"
                                % (name, seen[name], t))
            seen[name] = t
    # 分数合法性：必须落在更正后的第一档分数带 90-100 内，且随档位递减
    prev = None
    for t in sorted(TIER_SCORES):
        s = TIER_SCORES[t]
        if not (TOP_TIER_MIN <= s <= 100):
            raise TierError("第%d档分数 %d 不在第一档分数带 %d-100 内"
                            % (t, s, TOP_TIER_MIN))
        if prev is not None and s >= prev:
            raise TierError("档位分数必须随档位递减：第%d档 %d >= 第%d档 %d"
                            % (t, s, t - 1, prev))
        prev = s
    if len(seen) != 34:
        raise TierError("应收录 34 位诗人，当前 %d 位" % len(seen))
    return len(seen)


# ============================================================ 4. 名次（标准竞赛排名 1224）
def rank_records(records: list) -> list:
    """按 score 降序打名次；同分并列同一名次，后续名次跳到并列人数之后。

    并列内部按 姓名升序 -> id 升序，输出确定、可复现，不依赖 SQLite 行序。
    records 每项至少含 name/score；返回带 rank 字段的新列表（不修改入参）。
    """
    ordered = sorted(records, key=lambda r: (-r["score"], r["name"], r.get("id") or ""))
    out = []
    prev_score = None
    prev_rank = 0
    for i, r in enumerate(ordered):
        r = dict(r)
        if r["score"] == prev_score:
            r["rank"] = prev_rank
        else:
            r["rank"] = i + 1
            prev_rank = r["rank"]
            prev_score = r["score"]
        out.append(r)
    return out


# ============================================================ 5. DB 读取
def name_uid_map(con) -> dict:
    """{诗人名: uid}——只取「挂诗主行」（poems 实际指向的作者行）。

    与 build.py 的 author_uids 口径一致：李煜/刘禹锡这类同名多行，
    取有作品的那一行（南唐李煜 c9f56b57、刘禹锡 b460fdf1），空壳行不更新。
    """
    names = [n for n, _ in iter_poets()]
    if not names:
        return {}
    q = ",".join("?" * len(names))
    try:
        rows = con.execute(
            "SELECT a.name, a.uid, COUNT(p.id) AS n "
            "FROM authors a LEFT JOIN poems p ON p.author_uid=a.uid "
            "WHERE a.name IN (%s) GROUP BY a.uid" % q, names).fetchall()
    except sqlite3.Error as e:
        raise TierError("读取 authors 表失败：%s" % e)
    best = {}
    for name, uid, n in rows:
        cur = best.get(name)
        if cur is None or n > cur[1] or (n == cur[1] and uid < cur[0]):
            best[name] = (uid, n)
    return {name: uid for name, (uid, _n) in best.items()}


def pure_heat(con) -> dict:
    """{uid: {"name","score","n"}}——全部作者的**纯热度聚合分**（不含档位覆盖）。

    公式复用 author_heat.aggregate_author_scores（全仓唯一实现），从 poems.score
    重新聚合，不读可能被档位污染过的 authors.score / 热度 JSON。延迟导入
    author_heat：该模块顶部 import 本模块，模块级导入会形成循环。
    """
    import author_heat as AH
    try:
        rows = con.execute(
            "SELECT author_uid, score FROM poems WHERE score>0 AND author_uid<>''"
        ).fetchall()
        names = dict(con.execute("SELECT uid, name FROM authors").fetchall())
    except sqlite3.Error as e:
        raise TierError("读取主库失败：%s" % e)
    agg = AH.aggregate_author_scores(rows)
    return {uid: {"name": names.get(uid, ""), "score": v["score"], "n": v["n"]}
            for uid, v in agg.items()}


def heat_ranks(pure: dict) -> dict:
    """{uid: rank}——纯热度榜名次（1224，与 author_heat 导出口径一致）。"""
    ranked = rank_records([{"id": u, "name": v["name"], "score": v["score"]}
                           for u, v in pure.items()])
    return {r["id"]: r["rank"] for r in ranked}


def load_heat(path: str) -> dict:
    """读 author-heat-scores.json -> {uid: record}。文件不存在返回空 dict。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise TierError("读取 %s 失败：%s" % (path, e))
    return {r["id"]: r for r in d.get("authors", [])}


# ============================================================ 6. JSON 导出（原子写 + 回读校验）
def build_records(uid_map: dict, pure: dict) -> list:
    """合并 分级 + uid + 纯热度信息 -> poet-tiers.json 的 poets 数组（按排名序）。

    heat_score / heat_rank 是**覆盖前**的纯热度分与名次（留痕），库中无作者行
    或无热度分的诗人为 null。
    """
    ranks = heat_ranks(pure)
    records = []
    for name, c in iter_poets():
        uid = uid_map.get(name)
        h = pure.get(uid) if uid else None
        records.append({
            "id": uid,
            "name": name,
            "tier": c["tier"],
            "score": c["score"],
            "heat_score": h["score"] if h else None,
            "heat_rank": ranks.get(uid) if uid else None,
        })
    return rank_records(records)


def build_meta(records: list, uid_map: dict, stats: dict) -> dict:
    return {
        "name": "poet-tiers",
        "rubric_version": RUBRIC_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "key": "id=作者 uid（schema.make_author_uid 由 姓名+朝代 派生，重建不变）；"
               "库中无作者行的诗人 id=null",
        "record_fields": "id=作者uid / name=姓名 / score=档位分数 / tier=档位(1-5) / "
                         "rank=名次(1224) / heat_score,heat_rank=覆盖前的原始热度分与名次(留痕)",
        "rule": "五档分类与分数一一对应：第一档100 / 第二档98 / 第三档96 / "
                "第四档94 / 第五档92；同档诗人同分并列。按 score_band 更正后标准"
                "（第一档分数带=90-100），34 位诗人全部位于第 1 分数带",
        "formula": "score = TIER_SCORES[tier]（人工给定的档位分数）；"
                   "score_band(score)=1 对全部 34 位成立（分值 92-100 均在 90-100 内）",
        "score_mapping": {
            "band_source": "第一档分数带 90-100（score_band.py 更正后标准）；"
                           "档位分数由用户给定，不按槽位推导",
            "tiers": [
                {"tier": t, "label": TIER_LABELS[t], "score": TIER_SCORES[t],
                 "poets": list(TIER_POETS[t])}
                for t in sorted(TIER_POETS)
            ],
        },
        "tie_handling": "标准竞赛排名（1224）：同分并列同一名次，后续名次跳到并列人数之后；"
                        "并列内按 姓名升序 -> id 升序，输出确定可复现",
        "db": "poetry-pipeline/dist/poetry.db 的 authors.score 列；本文件 22 位有作者行的"
              "诗人分值与 DB 全量双向一致（12 位库中无作者行，仅存于本文件）",
        "stats": stats,
        "limitations": [
            "档位分数是人工分级的口径，会覆盖作者的热度聚合分；原始热度分"
            "留痕在本文件 heat_score/heat_rank 字段",
            "v1.0 的「第五档提升为第一档 + 内分 5 级 + 槽位取分数带上沿」标准"
            "已废弃不用；如见旧数据（如提升组 98-90、第二档 88 等分值）属历史产物",
            "同档诗人同分并列，不再按作品量或热度细分",
            "12 位诗人（第四档全部 + 闻一多/徐志摩/何其芳）在库中无作者行，"
            "不写入 DB；如后续语料收录其作品，重跑 --apply 即可补齐",
        ],
        "n_records": len(records),
    }


def export_json(path: str, records: list, meta: dict) -> str:
    """写 JSON：内存自检 -> 临时文件 -> 原子替换 -> 落盘回读全量比对。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "poets": records}, f, ensure_ascii=False,
                      separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise TierError("写入 %s 失败：%s" % (path, e))
    # 回读校验：防止写坏/截断，落盘即可信
    try:
        with open(path, encoding="utf-8") as f:
            back = json.load(f)
    except (OSError, ValueError) as e:
        raise TierError("回读 %s 失败：%s" % (path, e))
    if back.get("poets") != records:
        raise TierError("回读内容与内存态不一致（%s 可能损坏）" % path)
    return path


# ============================================================ 7. 同步：DB + 热度 JSON
def _guard_no_drift(con, pure: dict, tier_uids: set) -> None:
    """安全闸：非分级作者的当前分值必须与纯热度聚合一致，否则中止。

    分级诗人当前值可能是旧档位分（预期内，跳过）；其余作者若与 poems 重新
    聚合的结果有差异，说明库已被外部改动过——此时整库重置会覆盖那份数据，
    必须先中止让人工确认，而不是静默改写。唯一例外：当前值属于
    LEGACY_TIER_SCORES（旧 rubric 的输出），那是一次正常的标准更替残留，
    重置即可。将来若更换 rubric 启用新分值，把这个集合补全。
    """
    drift = []
    for uid, sc in con.execute("SELECT uid, score FROM authors").fetchall():
        if uid in tier_uids:
            continue
        expect = pure.get(uid, {}).get("score", 0)
        if sc != expect and sc not in LEGACY_TIER_SCORES:
            drift.append((uid, sc, expect))
    if drift:
        sample = "; ".join("%s: 库=%s 应为=%s" % (u[:8], a, b) for u, a, b in drift[:5])
        raise TierError("检测到 %d 位非分级作者的分值与 poems 聚合结果不一致"
                        "（如 %s）。库可能被外部改动，已中止——请先确认数据来源"
                        % (len(drift), sample))


def sync_db(con, records: list, pure: dict) -> int:
    """authors.score = 纯热度聚合分 + 档位覆盖（单事务 + 提交后复核）。

    先跑漂移安全闸；通过后整库重置为纯热度分，再按档位覆盖 22 位诗人。
    返回写入的行数（非零分行数）。
    """
    tier_uids = {r["id"] for r in records if r["id"]}
    _guard_no_drift(con, pure, tier_uids)

    final = {uid: v["score"] for uid, v in pure.items()}
    for r in records:
        if r["id"]:
            final[r["id"]] = r["score"]
    todo = [(s, uid) for uid, s in final.items() if s > 0]
    try:
        with con:
            con.execute("UPDATE authors SET score=0")
            con.executemany("UPDATE authors SET score=? WHERE uid=?", todo)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            con.executemany("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", [
                ("poet_tier_scored_at", now),
                ("poet_tier_rubric_version", RUBRIC_VERSION),
                ("poet_tier_count", str(len(records))),
                ("poet_tier_synced_rows", str(len(tier_uids))),
                ("poet_tier_json", "rank/poet-tiers.json"),
                ("poet_tier_note",
                 "authors.score = 纯热度聚合分 + 诗人分级档位分覆盖：第一档100 / "
                 "第二档98 / 第三档96 / 第四档94 / 第五档92（22 位有作者行的诗人）；"
                 "其余作者为热度聚合分。规则见 rank/poet-tiers.json"),
            ])
    except sqlite3.Error as e:
        raise TierError("写入 authors.score 失败（事务已回滚）：%s" % e)
    # 提交后复核，避免事务内自验的假阳性
    bad = 0
    for uid, s in final.items():
        row = con.execute("SELECT score FROM authors WHERE uid=?", (uid,)).fetchone()
        if not row or row[0] != s:
            bad += 1
    if bad:
        raise TierError("提交后复核发现 %d 位作者分与目标值不一致" % bad)
    return len(todo)


def sync_heat_json(path: str, records: list, pure: dict) -> tuple:
    """重建 author-heat-scores.json = 纯热度榜 + 22 位诗人的档位分，名次重算。

    从纯热度聚合（ poems 重新算）出发重建，而不是在旧文件上打补丁——
    旧文件可能带着已废弃标准的档位分。返回 (覆盖条数, 新增条数, 总条数)。
    原子写 + 回读校验。
    """
    final = {uid: {"name": v["name"], "score": v["score"]}
             for uid, v in pure.items()}
    overridden = added = 0
    for r in records:
        if not r["id"]:
            continue
        if r["id"] in final:
            final[r["id"]]["score"] = r["score"]
            overridden += 1
        else:
            final[r["id"]] = {"name": r["name"], "score": r["score"]}
            added += 1
    ranked = rank_records([{"id": u, "name": v["name"], "score": v["score"]}
                           for u, v in final.items()])
    out_records = [{"id": r["id"], "name": r["name"], "score": r["score"],
                    "rank": r["rank"]} for r in ranked]

    meta = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f).get("meta", {})
        except (OSError, ValueError) as e:
            raise TierError("读取 %s 失败：%s" % (path, e))
    meta = dict(meta)
    meta["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["tier_override"] = (
        "其中 %d 位诗人（纯热度覆盖 %d + 无热度分新增 %d）的 score 为诗人分级系统的"
        "档位分（第一档100/第二档98/第三档96/第四档94/第五档92），非热度聚合值；"
        "规则与原始热度分留痕见 rank/poet-tiers.json；名次已按覆盖后分值重算"
        % (overridden + added, overridden, added))
    meta["n_records"] = len(out_records)

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "authors": out_records}, f, ensure_ascii=False,
                      separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise TierError("写入 %s 失败：%s" % (path, e))
    try:
        with open(path, encoding="utf-8") as f:
            back = json.load(f)
    except (OSError, ValueError) as e:
        raise TierError("回读 %s 失败：%s" % (path, e))
    if back.get("authors") != out_records:
        raise TierError("回读内容与内存态不一致（%s 可能损坏）" % path)
    return overridden, added, len(out_records)


def backup(path: str, tag: str) -> str:
    """复制文件到同目录 <name>.bak-<tag>，返回备份路径。"""
    dst = "%s.bak-%s" % (path, tag)
    try:
        shutil.copy2(path, dst)
    except OSError as e:
        raise TierError("备份 %s 失败：%s" % (path, e))
    return dst


# ============================================================ 8. 一致性校验
def verify(con, records: list, pure: dict) -> list:
    """poet-tiers.json <-> DB <-> 热度JSON 三方全量比对，返回问题列表。"""
    problems = []
    heat_recs = load_heat(HEAT_JSON)
    # 1) 分级诗人：DB 与热度 JSON 的分值都必须等于档位分
    for r in records:
        if r["id"] is None:
            continue
        row = con.execute("SELECT score FROM authors WHERE uid=?", (r["id"],)).fetchone()
        if not row:
            problems.append("%s：DB 中找不到作者行 %s" % (r["name"], r["id"][:8]))
        elif row[0] != r["score"]:
            problems.append("%s：DB score=%s，档位分=%s" % (r["name"], row[0], r["score"]))
        h = heat_recs.get(r["id"])
        if h and h["score"] != r["score"]:
            problems.append("%s：热度JSON score=%s，档位分=%s"
                            % (r["name"], h["score"], r["score"]))
    # 2) 热度 JSON 双向：每条都能在 DB 找到同分作者（防残留/防丢失）
    for uid, h in heat_recs.items():
        row = con.execute("SELECT score FROM authors WHERE uid=?", (uid,)).fetchone()
        if not row:
            problems.append("热度JSON %s（%s）：DB 中无此作者行"
                            % (uid[:8], h.get("name")))
        elif row[0] != h["score"]:
            problems.append("热度JSON %s（%s）：score=%s，DB=%s"
                            % (uid[:8], h.get("name"), h["score"], row[0]))
    # 3) 纯热度交叉校验：有备份则比对 {uid: score}（确认 poems 未被外部改动）
    bak = HEAT_JSON + ".bak-20261017-171313"
    if os.path.exists(bak):
        try:
            with open(bak, encoding="utf-8") as f:
                old = {r["id"]: r["score"] for r in json.load(f).get("authors", [])}
        except (OSError, ValueError) as e:
            problems.append("读取纯热度备份失败：%s" % e)
        else:
            new = {u: v["score"] for u, v in pure.items()}
            if old != new:
                only_old = set(old) - set(new)
                only_new = set(new) - set(old)
                diff = [u for u in set(old) & set(new) if old[u] != new[u]]
                problems.append("纯热度与 17:13 备份不一致：备份独有 %d、现有独有 %d、"
                                "同 uid 不同分 %d（poems 或评分规则被改动过？）"
                                % (len(only_old), len(only_new), len(diff)))
    return problems


# ============================================================ 9. 报告
def report(records: list, uid_map: dict, stats: dict):
    print("\n-- 诗人分级（34 位，档位 -> 分数）--")
    for t in sorted(TIER_POETS):
        print("  %-5s（%3d 分）: %s"
              % (TIER_LABELS[t], TIER_SCORES[t], "、".join(sorted(TIER_POETS[t]))))
    print("\n-- 排行榜（rank 诗人 档位 分数 原始热度分/名次）--")
    for r in records:
        heat = ("%s/#%s" % (r["heat_score"], r["heat_rank"])
                if r["heat_score"] is not None else "-")
        print("  #%-3d %-5s 第%d档 score=%-3d heat=%s"
              % (r["rank"], r["name"], r["tier"], r["score"], heat))
    print("\n-- 统计 --")
    for k, v in stats.items():
        print("  %s: %s" % (k, v))


def main():
    global DB
    ap = argparse.ArgumentParser(description="诗人分级分类系统：五档分类与档位分数（v2.0）")
    ap.add_argument("--check", action="store_true", help="只校验分级数据自洽性")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--export", action="store_true", help="只写 rank/poet-tiers.json")
    ap.add_argument("--apply", action="store_true", help="导出 + 备份 + 同步 DB 与热度 JSON")
    # 默认库按脚本位置解析（HERE/dist），这对「在 poetry-pipeline 里直接跑 build.py」
    # 是对的；但构建若用了 `--out ./dist`（从项目根跑，产物在根的 dist/），默认路径
    # 会指向另一个库——2026-10-20 就因此把分写进了旧的 3.4 库、热度 JSON 也被
    # 按旧库数据重写。凡是换过 --out 的构建，事后必须显式传 --db。
    ap.add_argument("--db", default=DB, help="主库路径（默认 %s）" % DB)
    args = ap.parse_args()
    if not (args.check or args.dry_run or args.export or args.apply):
        ap.error("必须指定 --check / --dry-run / --export / --apply 之一")

    DB = os.path.abspath(args.db)

    try:
        print("=== 1. 分级数据自校验 ===")
        n = validate()
        print("  %d 位诗人，5 档，分数 %s，全部自洽"
              % (n, "/".join(str(TIER_SCORES[t]) for t in sorted(TIER_SCORES))))

        if args.check:
            print("check 通过")
            return

        print("=== 2. 读取主库，聚合纯热度分 ===")
        con = sqlite3.connect(DB)
        pure = pure_heat(con)
        uid_map = name_uid_map(con)
        records = build_records(uid_map, pure)
        n_with_row = len(uid_map)
        stats = {
            "n_poets": n,
            "n_with_db_row": n_with_row,
            "n_without_db_row": n - n_with_row,
            "no_db_row_poets": sorted(r["name"] for r in records if r["id"] is None),
            "n_pure_heat_authors": len(pure),
            "n_heat_score_before": sum(1 for r in records if r["heat_score"] is not None),
            "db_authors_total": con.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
        }
        print("  库中作者行 %d 个；纯热度聚合 %d 位；分级诗人中有作者行 %d 位、"
              "无作者行 %d 位、原有热度分 %d 位"
              % (stats["db_authors_total"], len(pure), n_with_row,
                 stats["n_without_db_row"], stats["n_heat_score_before"]))

        if args.dry_run:
            report(records, uid_map, stats)
            return

        print("=== 3. 导出 rank/poet-tiers.json ===")
        meta = build_meta(records, uid_map, stats)
        export_json(OUT, records, meta)
        print("  已写出 %s（%d 条，%.1f KB）"
              % (OUT, len(records), os.path.getsize(OUT) / 1024))

        if args.export:
            print("完成")
            return

        print("=== 4. 同步数据库与热度 JSON（先备份）===")
        tag = time.strftime("%Y%m%d-%H%M%S")
        bak_db = backup(DB, tag)
        print("  备份主库 -> %s" % os.path.basename(bak_db))
        bak_heat = backup(HEAT_JSON, tag)
        print("  备份热度 JSON -> %s" % os.path.basename(bak_heat))
        n_rows = sync_db(con, records, pure)
        print("  authors.score 已写入 %d 行非零分（含 22 位诗人档位分）" % n_rows)
        overridden, added, total = sync_heat_json(HEAT_JSON, records, pure)
        print("  热度 JSON：纯热度覆盖 %d + 无热度分新增 %d，共 %d 条，名次已重算"
              % (overridden, added, total))

        print("=== 5. 三方一致性校验 ===")
        problems = verify(con, records, pure)
        if problems:
            for p in problems:
                print("  [不一致] %s" % p)
            raise TierError("一致性问题 %d 条，请检查" % len(problems))
        print("  poet-tiers.json / DB / 热度JSON 三方 %d 位有行诗人全部一致" % n_with_row)
        print("完成")
    except TierError as e:
        print("\n[失败] %s" % e, file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print("\n[失败] 数据库错误：%s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
