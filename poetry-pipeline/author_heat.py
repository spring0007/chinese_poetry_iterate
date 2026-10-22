#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作者网络热度评分（「热门作者」）——「热门诗词」四维模型在作者维度上的聚合。

背景
----
poems.score 是 heat_score.py 产出的四维网络热度综合分（1-100）：
    20 × (0.30·传播广度 + 0.20·时间跨度 + 0.25·接受深度 + 0.25·当代热度)
本工具把同一套模型聚合到作者维度：一位作者的热度 = 其名下全部**有效评分诗词**
综合分的收缩均值（向全局均值回归，压制单首小样本噪音，公式见下）。

评分规则（rubric 1.0）
====================
公式
    作者分 = round_half_up( (Σscoreᵢ + k·μ) / (n + k) )，取值 1-100
    Σscoreᵢ = 该作者全部有效评分诗词的 poems.score 之和；n = 这些诗的首数；
    μ = 全部计入诗词的全局均分（先验）；k = PRIOR_K = 20（先验强度）。
    round_half_up 为四舍五入（int(x + 0.5)，不用 round() 的银行家舍入）。

为什么要收缩项（而不是纯算术平均）
    本库 13,056 位有热度分的作者里，6,900 位（53%）只有 1 首。纯均值下
    「一首诗的运气」就是「作者的热度」——榜首会被单首名作的陌生作者占满
    （实测：雷震、鲁等 n=1 作者均分 98，压过李白 74）。收缩估计让作者分向
    全局均值回归：n 越小回归越多，n 越大越接近自身均值。k=20 时，n=1 的
    作者最高只能到 (100+20μ)/21 ≈ 56 分，进不了名家区（61-75）；而 n≥100
    的作者几乎不受影响（苏轼 61.1 -> 61.1）。一个参数，无阈值悬崖，
    也不会把作品少的名家（张若虚 n=3）直接排除在榜外。

与四维模型的关系
    诗词综合分 = 20×(0.30·广度 + 0.20·跨度 + 0.25·深度 + 0.25·当代热度)，
    是四个维度的**线性**函数，因此 mean(20·Σwᵢ·dᵢ) = 20·Σwᵢ·mean(dᵢ)：
    「平均诗词分」与「先平均四维再综合」等价（仅差逐首取整的 ≤0.5 噪声）。
    收缩项只作用于这个均值之上，不改变任何维度权重。

参数
    - 有效评分诗词：poems.score > 0 且 author_uid 非空。
      author_uid='' 是佚名诗（JOIN 不上任何作者行），不参与评分。
    - 入选门槛：至少 1 首有效评分诗词。0 首的作者不进 JSON，DB 中 score=0。
    - 取值范围：收缩是 [1,100] 区间内的凸组合，结果必在界内；仍逐条断言。

并列名次
    标准竞赛排名（competition ranking，「1224」）：综合分相同的作者并列同一
    名次，其后名次跳到并列人数之后（1, 2, 2, 4 …）。并列内部按
    姓名升序 -> uid 升序，输出确定、可复现，不依赖 SQLite 行序。

产物
====
    rank/author-heat-scores.json   {meta: 规则与统计, authors: [{id,name,score,rank}]}
                                   仅含有有效评分的作者；id（= authors.uid）唯一。
                                   注意 id 是 uid 不是 authors.id——后者是导入顺序
                                   序号，源库增删一位作者就整体位移。
    dist/poetry.db                 authors 表新增 score 列（ALTER TABLE，幂等），
                                   写入与 JSON 完全一致的评分；meta 表写溯源键。

一致性保证（任一步失败都不留半成品）
==================================
    - aggregate_author_scores() 是全仓唯一实现：author_heat.py（读主库聚合）与
      build.py（内存 poems 聚合）都调它，公式不会两处漂移；
    - JSON 采用「临时文件 + os.replace」原子写，落盘后回读与内存态全量比对，
      通过之后才动数据库；
    - 数据库写入在单事务内完成（清零旧分 -> 写新分 -> 写 meta -> 事务内校验），
      任一不符整体回滚；提交后重开连接再复核；
    - JSON 与 DB 做**双向**比对（JSON->DB、DB->JSON），任何方向不一致都以
      非零码退出并保留备份。

用法
----
    python author_heat.py --dry-run   # 只算不写：覆盖率/星级分布/Top20/并列样例
    python author_heat.py --export    # 只写 JSON
    python author_heat.py --apply     # 导出 + 备份主库 + ALTER + 写入 + 全量校验
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sqlite3
import sys
import time

import poet_tiers

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # E:/chinese_poetry_iterate
DB = os.path.join(HERE, "dist", "poetry.db")
RANK_DIR = os.path.join(ROOT, "rank")
OUT = os.path.join(RANK_DIR, "author-heat-scores.json")

RUBRIC_VERSION = "1.0"
# 与 heat_score.py 的 WEIGHTS 一致：作者分是诗词四维综合分的聚合，不另设权重。
WEIGHTS = {"breadth": 0.30, "span": 0.20, "depth": 0.25, "now": 0.25}
STAR_OF = ((90, 5), (75, 4), (55, 3), (35, 2), (0, 1))
# 收缩先验强度 k：等效「每位作者先自带 k 首全局均分的影子诗」。
# k=20 的实测效果：n=1 作者最高 (100+20μ)/21 ≈ 56 分，进不了名家区（61-75）；
# n≥100 的作者几乎不受影响。k=0 即退化为纯算术平均（供对比/测试）。
PRIOR_K = 20

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


class HeatError(Exception):
    """评分/导出/落库任一步骤的确定性失败。消息里带阶段信息，便于定位。"""


def warn(msg: str):
    print("  [警告] " + msg)


# ============================================================ 1. 核心计算（全仓唯一实现）
def aggregate_author_scores(pairs, prior_k: int = PRIOR_K) -> dict:
    """(author_uid, poem_score) 流 -> {author_uid: {"score": int, "n": int}}。

    单一实现：author_heat.py（读主库）与 build.py（内存 poems）都调它，
    保证「JSON 里的分」与「重建出来的分」永远是同一个公式算出来的。

    规则：只统计 score>0（有有效热度评分）且 author_uid 非空（挂了作者行，
    佚名不参与）的诗；作者分 = 收缩均值
        round_half_up( (Σscore + k·μ) / (n + k) )
    μ 为全局均分，k 为 prior_k。单次遍历两阶段：先累计和与首数，拿到 μ 后
    统一结算——调用方只管喂 (uid, score)，不用自己算先验。
    """
    acc = collections.defaultdict(lambda: [0, 0])      # uid -> [分数和, 首数]
    grand_total, grand_n = 0, 0
    for uid, score in pairs:
        # 防御性过滤：非 int（含 bool）/ 非正数一律不计入。DB 列是 INTEGER、
        # build 侧 p.score 也是 int，正常走不到这里；走到说明上游数据坏了，
        # 静默计入会把错误放大成错误的热度排名。
        if not uid or not isinstance(score, int) or isinstance(score, bool) or score <= 0:
            continue
        cell = acc[uid]
        cell[0] += score
        cell[1] += 1
        grand_total += score
        grand_n += 1
    if not acc:
        return {}
    mu = grand_total / grand_n                        # 全局均分（收缩先验）
    out = {}
    for uid, (total, n) in acc.items():
        if n <= 0:
            continue                                   # 防御：acc 里不该有 0 首的键
        s = int((total + prior_k * mu) / (n + prior_k) + 0.5)   # 四舍五入（half-up）
        if not (1 <= s <= 100):
            raise HeatError("作者 %s 收缩均分 %d 越界（应为 1-100），上游诗词分异常" % (uid, s))
        out[uid] = {"score": s, "n": n}
    return out


def rank_records(rich: list) -> list:
    """rich: [{id,name,dynasty,score,n}] -> JSON 记录 [{id,name,score,rank}]。

    并列名次：标准竞赛排名（1224）——同分并列同一名次，后续名次跳跃；
    并列内按 姓名升序 -> uid 升序，保证输出确定可复现。
    """
    ordered = sorted(rich, key=lambda r: (-r["score"], r["name"], r["id"]))
    records = []
    prev_score, prev_rank = None, 0
    for i, r in enumerate(ordered):
        # 与上一条同分 -> 继承上一条名次；否则名次 = 排序位置（1 基）
        rank = prev_rank if (prev_score is not None and r["score"] == prev_score) else i + 1
        records.append({"id": r["id"], "name": r["name"], "score": r["score"], "rank": rank})
        prev_score, prev_rank = r["score"], rank
    return records


def validate_records(records: list):
    """落盘/落库前的断言：id 唯一、分值 1-100、名次合规（同分同名次、异分名次严格递增）。"""
    if not records:
        raise HeatError("没有任何作者获得有效评分——主库 poems.score 可能全是 0"
                        "（先运行 heat_score.py --apply）")
    ids = [r["id"] for r in records]
    dup = len(ids) - len(set(ids))
    if dup:
        raise HeatError("作者 id 不唯一：%d 条记录里有 %d 个重复 id" % (len(ids), dup))
    if records[0]["rank"] != 1:
        raise HeatError("排名未从 1 开始（首条 rank=%s）" % records[0]["rank"])
    prev = None
    for r in records:
        if not isinstance(r["score"], int) or isinstance(r["score"], bool) \
                or not (1 <= r["score"] <= 100):
            raise HeatError("作者 %s 的分值异常：%r" % (r["id"], r["score"]))
        if prev is not None:
            if r["score"] == prev["score"]:
                if r["rank"] != prev["rank"]:
                    raise HeatError("同分不同名次：%s(%d,%d) vs %s(%d,%d)"
                                    % (prev["id"], prev["score"], prev["rank"],
                                       r["id"], r["score"], r["rank"]))
            elif r["rank"] <= prev["rank"]:
                raise HeatError("名次非严格递增：%s(rank=%d) -> %s(rank=%d)"
                                % (prev["id"], prev["rank"], r["id"], r["rank"]))
        prev = r


# ============================================================ 2. 读数
def load_from_db(con) -> tuple:
    """主库 -> (rich 记录输入, 统计信息)。

    只统计有有效热度分且挂了作者的诗；佚名与孤儿 author_uid 单独计数上报。
    """
    try:
        n_total = con.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        n_anon = con.execute(
            "SELECT COUNT(*) FROM poems "
            "WHERE score>0 AND (author_uid IS NULL OR author_uid='')").fetchone()[0]
        n_orphan = con.execute(
            "SELECT COUNT(DISTINCT p.author_uid) FROM poems p "
            "LEFT JOIN authors a ON a.uid=p.author_uid "
            "WHERE p.score>0 AND p.author_uid<>'' AND a.uid IS NULL").fetchone()[0]
        rows = con.execute(
            "SELECT a.uid, a.name, a.dynasty, p.score "
            "FROM poems p JOIN authors a ON a.uid=p.author_uid "
            "WHERE p.score>0 AND p.author_uid<>''").fetchall()
    except sqlite3.Error as e:
        raise HeatError("读取主库失败：%s" % e)
    if n_orphan:
        warn("%d 个 author_uid 在 authors 表里找不到对应行（孤儿关联），这些诗不计入任何作者"
             % n_orphan)
    agg = aggregate_author_scores((uid, score) for uid, _n, _d, score in rows)
    info = {uid: (name, dynasty) for uid, name, dynasty, _s in rows}
    rich = [{"id": uid, "name": info[uid][0], "dynasty": info[uid][1],
             "score": v["score"], "n": v["n"]} for uid, v in agg.items()]
    # 诗人分级覆盖层（poet_tiers）：34 位人工分级诗人按档位改分（五档分类与
    # 分数一一对应：100/98/96/94/92）；其中无热度分诗词的名家（屈原等）补一条
    # n=0 记录按档位分进榜；库中无作者行的 12 位跳过。规则与留痕见 rank/poet-tiers.json。
    rich, n_tier_ovr, n_tier_add = poet_tiers.apply_overrides_rich(rich, con)
    stats = {
        "n_authors_total": n_total,
        "n_authors_scored": len(rich),
        "n_authors_excluded": n_total - len(rich),
        "n_scored_poems": sum(v["n"] for v in agg.values()),
        "n_anonymous_poems": n_anon,
        "n_orphan_author_uids": n_orphan,
        "n_tier_overridden": n_tier_ovr,
        "n_tier_added": n_tier_add,
        "global_mean": round(sum(s for _u, _n, _d, s in rows) / len(rows), 2) if rows else 0,
    }
    return rich, stats


# ============================================================ 3. JSON 导出（原子写 + 回读校验）
def build_meta(stats: dict, n_records: int) -> dict:
    meta = {
        "name": "author-heat-scores",
        "rubric_version": RUBRIC_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "key": "作者 uid（schema.make_author_uid 由 姓名+朝代 派生，重建不变；"
               "不是会随导入顺序位移的 authors.id）",
        "record_fields": "id=作者uid / name=作者名 / score=综合热度分(1-100) / rank=名次(1起)",
        "formula": "作者分 = round_half_up((Σscoreᵢ + k·μ)/(n + k))，取值 1-100；"
                   "Σscoreᵢ=该作者有效评分诗词分之和，n=首数，μ=全局均分，k=PRIOR_K",
        "equivalence": "诗词综合分是四维的线性函数，mean(20·Σw·d)=20·Σw·mean(d)，"
                       "故等价于「先平均四维再综合」（仅取整噪声）；"
                       "收缩项只作用于该均值之上，不改变维度权重",
        "params": {
            "source": "poems.score（四维网络热度综合分 1-100，与 rank/heat-scores.json 同源）",
            "valid_poem": "poems.score>0 且 author_uid 非空；佚名诗(author_uid='')不参与",
            "min_poems": 1,
            "prior_k": PRIOR_K,
            "prior_mu": "全局均分（见 global_mean）",
            "rounding": "half-up（int(x+0.5)）",
            "shrinkage_rationale": "53% 的作者只有 1 首有热度分的诗，纯均值下"
                                   "「一首诗的运气」就是「作者的热度」；向全局均值"
                                   "收缩后，n=1 作者最高约 56 分（名家区 61-75），"
                                   "n≥100 的作者几乎不受影响",
        },
        "weights": WEIGHTS,
        "star_thresholds": {"5": 90, "4": 75, "3": 55, "2": 35, "1": 0},
        "tie_handling": "标准竞赛排名（1224）：同分并列同一名次，后续名次跳到并列人数之后；"
                        "并列内按 姓名升序 -> uid 升序，输出确定可复现",
        "db": "poetry-pipeline/dist/poetry.db 的 authors.score 列，与本文件全量双向一致",
        "credibility": "未联网实时查询。作者分由诗词分聚合而来，诗词分来自历史搜索引擎"
                       "快照与库内结构化证据的规则化估算（见 rank/heat-meta.json）；"
                       "反映的是抓取时点的热度结构，非当前实时指数。",
        "limitations": [
            "作者分是收缩均值口径（k=PRIOR_K=20）：作品极少的作者向全局均值回归，"
            "不会因单首名作登顶；作品多的作者几乎等于自身均值",
            "同姓名跨朝代的作者按 (姓名,朝代) 区分，各自独立计分",
            "仅统计有热度分的 tang-poem/song-poem/song-ci 三集合；其余集合诗词 score=0，不参与",
        ],
        **stats,
        "n_records": n_records,
    }
    # 诗人分级覆盖说明：有档位覆盖时必须在 meta 里讲清楚，否则「热度分」的
    # 口径会被误读——那 22 位诗人的分不是聚合值，是人工档位分。
    if stats.get("n_tier_overridden") or stats.get("n_tier_added"):
        meta["tier_override"] = (
            "其中 %d 位诗人（覆盖 %d + 新增 %d）的 score 为诗人分级系统的档位分，"
            "非热度聚合值；规则、级别与原始热度分留痕见 rank/poet-tiers.json"
            % (stats.get("n_tier_overridden", 0) + stats.get("n_tier_added", 0),
               stats.get("n_tier_overridden", 0), stats.get("n_tier_added", 0)))
    return meta


def export_json(records: list, stats: dict) -> str:
    """写 rank/author-heat-scores.json：内存态先自检 -> 原子写 -> 落盘回读全量比对。"""
    payload = {"meta": build_meta(stats, len(records)), "authors": records}
    try:
        os.makedirs(RANK_DIR, exist_ok=True)
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, OUT)
    except OSError as e:
        if os.path.exists(OUT + ".tmp"):
            try:
                os.remove(OUT + ".tmp")
            except OSError:
                pass
        raise HeatError("写 JSON 失败（%s）：%s" % (OUT, e))
    # 落盘后回读校验：写坏了必须在动数据库之前发现，否则 DB 会跟着坏
    try:
        with open(OUT, encoding="utf-8") as f:
            back = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise HeatError("JSON 回读校验失败：%s" % e)
    if back != payload:
        raise HeatError("JSON 回读内容与内存态不一致（写入损坏？），已保留现场文件：%s" % OUT)
    validate_records(back["authors"])
    return OUT


# ============================================================ 4. 数据库变更与同步
def verify_db_matches(con, records: list) -> list:
    """DB  authors.score 与 records 的双向比对，返回不一致条目（空 = 一致）。

    双向：JSON 里有的作者，DB 必须同分；DB 里 score>0 的作者，必须都在 JSON 里。
    只比一个方向会漏掉「DB 残留旧分」——那会让榜单出现 JSON 中不存在的作者。
    """
    db = {uid: (sc or 0) for uid, sc in con.execute("SELECT uid, score FROM authors")}
    want = {r["id"]: r["score"] for r in records}
    bad = []
    for uid in set(db) | set(want):
        d, w = db.get(uid, 0) or 0, want.get(uid, 0) or 0
        if d != w:
            bad.append((uid, d, w))
    return bad


def apply_db(records: list, stats: dict) -> str:
    """备份 -> 幂等 ALTER -> 单事务写入 -> 事务内校验 -> 提交后复核。返回备份路径。"""
    if not os.path.exists(DB):
        raise HeatError("找不到主库 %s" % DB)
    try:
        bak = DB + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB, bak)
        print("  备份主库 -> %s（%.1f MB）" % (bak, os.path.getsize(DB) / 1048576))
    except OSError as e:
        raise HeatError("备份主库失败（不动库）：%s" % e)

    con = sqlite3.connect(DB)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        # 1) 表结构变更（幂等）：已有 score 列就跳过，重复 ALTER 会报 duplicate column
        cols = [r[1] for r in con.execute("PRAGMA table_info(authors)")]
        if "score" not in cols:
            try:
                con.execute("ALTER TABLE authors ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
                con.commit()
                print("  authors 表新增 score 列：ALTER TABLE authors "
                      "ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
            except sqlite3.Error as e:
                raise HeatError("ALTER TABLE 失败（库未改动）：%s" % e)
        else:
            print("  authors.score 列已存在，跳过 ALTER")
        # 热度榜索引：与 poems 侧的 ix_poems_score 对称，ORDER BY a.score DESC 走它。
        # ALTER 路径同样要建——否则「apply 出来的库」和「重建出来的库」结构不一致，
        # 后者有索引而前者没有，查询计划会随库的来源不同而漂移。
        try:
            con.execute("CREATE INDEX IF NOT EXISTS ix_authors_score ON authors(score DESC)")
            con.commit()
            print("  索引 ix_authors_score 就绪（IF NOT EXISTS，幂等）")
        except sqlite3.Error as e:
            raise HeatError("创建 ix_authors_score 失败：%s" % e)

        # 2) 数据事务：清零旧分 -> 写新分 -> 写 meta -> 事务内校验，全过才提交。
        #    清零是必须的：不清零的话，上一次算过分、这次不再符合条件的作者会残留旧分，
        #    DB 与 JSON 从此对不上。
        try:
            with con:
                con.execute("UPDATE authors SET score=0")
                con.executemany("UPDATE authors SET score=? WHERE uid=?",
                                [(r["score"], r["id"]) for r in records])
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                for k, v in (
                    ("author_heat_scored_at", now),
                    ("author_heat_rubric_version", RUBRIC_VERSION),
                    ("author_heat_count", str(len(records))),
                    ("author_heat_json", "rank/author-heat-scores.json"),
                    ("author_heat_note",
                     "authors.score = 作者网络热度综合分 1-100（其有效评分诗词 "
                     "poems.score 的收缩均值：向全局均值回归后四舍五入，k=20）；"
                     "0 = 无有效评分诗词；"
                     "公式/参数/并列名次见 rank/author-heat-scores.json 的 meta"),
                ):
                    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
                bad = verify_db_matches(con, records)
                if bad:
                    raise HeatError("事务内校验发现 %d 条不一致，已整体回滚；样例：%s"
                                    % (len(bad), bad[:5]))
        except HeatError:
            raise
        except sqlite3.Error as e:
            raise HeatError("数据库写入失败（已回滚，库回到写入前状态）：%s" % e)

        # 3) 提交后复核：重开连接，防同连接缓存造成的「假一致」
        bad = verify_db_matches(con, records)
        if bad:
            raise HeatError("提交后复核仍不一致 %d 条：%s（备份在 %s）"
                            % (len(bad), bad[:5], bak))
        n_db = con.execute("SELECT COUNT(*) FROM authors WHERE score>0").fetchone()[0]
        print("  authors.score：写入 %d 条（DB 非零 %d 条，与 JSON %d 条一致）"
              % (len(records), n_db, len(records)))
    except sqlite3.Error as e:
        raise HeatError("数据库操作失败：%s" % e)
    finally:
        con.close()
    return bak


# ============================================================ 5. 试评报告
def report(rich: list, records: list, stats: dict):
    print("\n-- 作者评分（收缩均值，μ=%.2f，k=%d）--"
          % (stats["global_mean"], PRIOR_K))
    print("  作者分 = round((Σscoreᵢ + k·μ)/(n + k))；n 为该作者有效评分诗词数")
    print("  作者 %d 位：获得有效评分 %d 位；未获评分 %d 位（DB score=0）"
          % (stats["n_authors_total"], stats["n_authors_scored"],
             stats["n_authors_excluded"]))
    print("  计入的诗词 %d 首（另有佚名 %d 首不参与；孤儿 author_uid %d 个）"
          % (stats["n_scored_poems"], stats["n_anonymous_poems"],
             stats["n_orphan_author_uids"]))

    hist = collections.Counter(next(s for th, s in STAR_OF if r["score"] >= th)
                               for r in records)
    print("\n  星级分布（按诗词阈值 90/75/55/35 折算，仅诊断用，不入库）:")
    for s in (5, 4, 3, 2, 1):
        print("    %d星 %6d 位 (%.1f%%)" % (s, hist[s], 100.0 * hist[s] / len(records)))

    by_uid = {r["id"]: r for r in rich}
    print("\n  Top 20（rank 作者 朝代 作者分 基于N首）:")
    for r in records[:20]:
        d = by_uid[r["id"]]
        print("    #%-3d %-10s %-4s %3d 分（%d 首）"
              % (r["rank"], r["name"], d["dynasty"], r["score"], d["n"]))

    # 并列名次样例：找第一个人数>1 的名次簇，展示 1224 处理
    clusters = collections.defaultdict(list)
    for r in records:
        clusters[r["rank"]].append(r)
    tie = next(((rk, rs) for rk, rs in sorted(clusters.items()) if len(rs) > 1), None)
    if tie:
        rk, rs = tie
        first_idx = next(i for i, r in enumerate(records) if r["rank"] == rk)
        nxt_i = first_idx + len(rs)          # 并列簇之后那一条（可能已越界=末位并列）
        print("\n  并列名次样例（标准竞赛排名 1224）:")
        print("    rank %d 并列 %d 位：%s（同分 %d）"
              % (rk, len(rs), "、".join(r["name"] for r in rs), rs[0]["score"]))
        if nxt_i < len(records):
            print("    -> 下一名次为 rank %d（%s），不重号也不跳号"
                  % (records[nxt_i]["rank"], records[nxt_i]["name"]))
        else:
            print("    -> 该并列簇位于榜尾，无后续名次")


# ============================================================ 入口
def main():
    global DB
    ap = argparse.ArgumentParser(description="作者网络热度评分（热门作者）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--export", action="store_true", help="只写 JSON")
    ap.add_argument("--apply", action="store_true",
                    help="导出 + 备份主库 + ALTER + 写入 + 全量校验")
    # 同 poet_tiers.py：默认库是 HERE/dist，构建若用了 `--out ./dist`（从项目根跑）
    # 必须显式指定，否则会拿另一个库重算并覆盖 rank/author-heat-scores.json。
    ap.add_argument("--db", default=DB, help="主库路径（默认 %s）" % DB)
    args = ap.parse_args()
    if not (args.dry_run or args.export or args.apply):
        ap.error("必须指定 --dry-run / --export / --apply 之一")

    DB = os.path.abspath(args.db)

    try:
        if not os.path.exists(DB):
            raise HeatError("找不到主库 %s（作者分从 poems.score 聚合，先构建主库）" % DB)
        con = sqlite3.connect(DB)
        try:
            pcols = [r[1] for r in con.execute("PRAGMA table_info(poems)")]
            if "score" not in pcols:
                raise HeatError("主库 poems 表没有 score 列：先运行 heat_score.py --apply "
                                "完成诗词热度重评，作者分才能聚合")
            print("=== 1. 读取主库并聚合 ===")
            rich, stats = load_from_db(con)
        finally:
            con.close()
        print("  作者 %d 位获得有效评分（基于 %d 首有热度分的诗词）"
              % (stats["n_authors_scored"], stats["n_scored_poems"]))

        print("=== 2. 排名 ===")
        records = rank_records(rich)
        validate_records(records)          # 任何一步异常都在此之前中止，不写任何文件
        print("  %d 条记录，名次 1-%d" % (len(records), records[-1]["rank"]))

        if args.dry_run:
            report(rich, records, stats)
            print("\n--dry-run：未写任何文件 --")
            return

        print("=== 3. 导出 JSON ===")
        out = export_json(records, stats)
        print("  已写出 %s（%.1f KB，%d 条）"
              % (out, os.path.getsize(out) / 1024, len(records)))

        if args.apply:
            print("=== 4. 同步到数据库 ===")
            apply_db(records, stats)
        print("完成")
    except HeatError as e:
        print("\n[失败] %s" % e, file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        # 库被锁 / 磁盘错误 / 连接异常等：不留半栈，明确报阶段
        print("\n[失败] SQLite 错误（库应未被改动或已回滚）：%s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
