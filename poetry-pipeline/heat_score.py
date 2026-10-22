#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诗词网络热度重评工具（替代 rank/ 下已过期的搜索引擎快照评分）。

背景
----
rank/ 目录原是五引擎（百度/必应/360/谷歌）搜索结果数快照，旧构建把它 log 量化成
poems.score（0-255）。该快照已过期，且粒度粗（同作者同题名共享一个值）。
本工具按「古诗词网络热度分析助手」提示词的四维模型重新评估：

  传播广度  五引擎命中数 log 均值的分档（+国际覆盖奖励）
  时间跨度  朝代经典化先验 + 选本/赏析证据修正
  接受深度  库内学术 apparatus（译文/注释/赏析/简介/配图）加权
  当代热度  百度+360 中国向当前热度 log 加权分档

综合热度分 = 20 × (0.30·广度 + 0.20·跨度 + 0.25·深度 + 0.25·当代)，取值 1-100；
星级：≥90 ★5 / ≥75 ★4 / ≥55 ★3 / ≥35 ★2 / 其余 ★1。

诚实性约束（提示词要求）：
  - 未联网实时查询；搜索引擎数字来自历史快照，仅作热度证据，不编造精确值；
  - 同作者同题名/词牌的多首作品共享估值（rank 数据无正文，无法逐首区分）；
  - 无 rank 快照的集合（诗经/楚辞/近现代等）不参与本次评估，score 保持 0。

产物格式（2026-10-16 起为扁平结构）
----
  rank/heat-scores.json        扁平 {uid: 综合热度分(1-100)}，uid 字典序每行一条，O(1) 读写
  rank/heat-meta.json          评分规则/权重/档位/星级阈值/可信度说明
  heat-scores-details.json     富明细（四维分/星级/评语/引擎证据），仅 --details 时导出

用法
----
  python heat_score.py --dry-run   # 只算不写：覆盖率/星级分布/Top50/边界样例
  python heat_score.py --export    # 写扁平 heat-scores.json + heat-meta.json
  python heat_score.py --export --details   # 另存富明细 heat-scores-details.json
  python heat_score.py --apply     # 导出 + 备份主库 + 覆盖 poems.score + 写 meta
  python heat_score.py --flatten   # 旧嵌套 heat-scores.json -> 扁平格式（不重算，无需 rank 快照）
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import shutil
import sqlite3
import sys
import time

from zhconv import convert

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # E:/chinese_poetry_iterate
DB = os.path.join(HERE, "dist", "poetry.db")
RANK_DIR = os.path.join(ROOT, "rank")
OUT = os.path.join(RANK_DIR, "heat-scores.json")          # 扁平 {uid: 综合分}
META_OUT = os.path.join(RANK_DIR, "heat-meta.json")       # 规则/权重/阈值/可信度
DETAILS_OUT = os.path.join(RANK_DIR, "heat-scores-details.json")  # 富明细（可选）

RUBRIC_VERSION = "1.0"
SCORED_COLLS = ("tang-poem", "song-poem", "song-ci")   # 有 rank 快照的三个集合
ENGINES = ("baidu", "so360", "bing", "bing_en", "google")

# ---------------------------------------------------------------- 折叠
SPACES = (" ", "\u3000", "\t", "\xa0", "\u2007", "\u202f")
ANON = {"不详", "佚", "匿名", "无名", "无名的", "失名", "阙名", "闕名"}
ANON_TOKENS = ("佚名", "无名", "失名", "阙名")


def fold(s: str) -> str:
    """简体归一 + 去空格（与 schema.norm_text 同规则；匿名一律折成空串）。"""
    if not s:
        return ""
    prev = s
    for _ in range(3):
        cur = convert(prev, "zh-cn")
        if cur == prev:
            break
        prev = cur
    for ch in SPACES:
        if ch in prev:
            prev = prev.replace(ch, "")
    if prev in ANON:
        return ""
    for t in ANON_TOKENS:
        if t in prev:
            return ""
    return prev


# ---------------------------------------------------------------- 档位表
# 依据五引擎 log 均值实测分位数标定（p25=2.65 / p50=3.11 / p75=3.57 / p95=4.38）。
def tier(logv: float) -> float:
    if logv >= 5.0:
        return 5.0
    if logv >= 4.4:
        return 4.5
    if logv >= 3.6:
        return 4.0
    if logv >= 3.1:
        return 3.0
    if logv >= 2.65:
        return 2.0
    return 1.5


# 朝代经典化先验（时间跨度基础分）。唐/宋为历代选本收录之最。
SPAN_BASE = {
    "preqin": 5.0, "tang": 4.5, "song": 4.5, "han": 4.0, "wudai": 4.0,
    "weijin": 4.0, "nanbeichao": 4.0, "sui": 4.0, "jin": 3.5, "yuan": 3.5,
    "ming": 3.0, "qing": 3.0, "modern": 2.0, "unknown": 2.5, "custom": 2.5,
}

WEIGHTS = {"breadth": 0.30, "span": 0.20, "depth": 0.25, "now": 0.25}
STAR_OF = ((90, 5), (75, 4), (55, 3), (35, 2), (0, 1))

DYN_LABEL = {"tang": "唐", "song": "宋", "preqin": "先秦", "han": "汉", "wudai": "五代",
             "weijin": "魏晋", "nanbeichao": "南北朝", "sui": "隋", "jin": "金",
             "yuan": "元", "ming": "明", "qing": "清", "modern": "近现代",
             "unknown": "未详", "custom": "自建"}
KIND_LABEL = {"poem": "诗", "ci": "词", "qu": "曲", "prose": "文",
              "classic": "经", "fu": "赋", "article": "论著"}

STAR_COMMENT = {
    5: "全网热度极高的跨时代经典，全民熟知。",
    4: "热度很高，在特定时期或平台有广泛传播，大众熟知。",
    3: "有一定热度，在诗词爱好者中广为流传。",
    2: "热度一般，主要被特定群体关注。",
    1: "热度较低，网络讨论和搜索量都较少。",
}

META = {
    "name": "heat-scores",
    "rubric_version": RUBRIC_VERSION,
    "generated_at": None,  # 运行时填
    "key": "诗词 uid（schema.make_uid 内容派生，重建不变）",
    "source": "rank/ 五引擎搜索结果快照（百度/必应中国/必应国际/360/谷歌），"
              "抓取于历史快照、已过期，仅作热度证据",
    "online_query": False,
    "credibility": "未联网实时查询。评分由既有搜索引擎快照与库内结构化证据"
                    "（译文/注释/赏析/简介/配图）按四维模型规则化估算，可复现；"
                    "不编造精确指数，历史快照数字可能与当前实际有出入。",
    "dims": {"b": "传播广度", "s": "时间跨度", "p": "接受深度", "n": "当代热度"},
    "weights": WEIGHTS,
    "tiers": "五引擎 log10(1+命中) 均值分档：>=5.0->5, >=4.4->4.5, >=3.6->4.0, "
             ">=3.1->3.0, >=2.65->2.0, 其余 1.5；当代热度用 0.6*log(百度)+0.4*log(360)",
    "span_rule": "朝代先验：" + "; ".join("%s=%s" % (k, v) for k, v in SPAN_BASE.items())
                 + "；带译文且带赏析 +0.5（选本/教材证据，封顶 5）",
    "depth_rule": "1.5 + 0.75*译文 + 0.75*注释 + 1.0*赏析 + 0.5*(简介|背景) + 0.5*配图，封顶 5",
    "star_thresholds": {"5": 90, "4": 75, "3": 55, "2": 35, "1": 0},
    "composite": "20 * (0.30*广度 + 0.20*跨度 + 0.25*深度 + 0.25*当代)，1-100",
    "limitations": [
        "同作者同题名/词牌的多首作品共享热度估值（rank 无正文，无法逐首区分，与旧构建同口径）",
        "仅评估有 rank 快照的 tang-poem/song-poem/song-ci 三个集合；其余集合 score=0",
        "搜索引擎快照为历史数据，反映的是抓取时点的热度结构",
    ],
    "n_scored": 0,
    "scores_file": "heat-scores.json",
    "format": "扁平 JSON：{uid: 综合热度分(1-100 整数)}，uid 字典序、每行一条，便于按 uid O(1) 读写",
    "details_file": None,   # --details 时填 "heat-scores-details.json"
}


def warn(msg: str):
    print("  [警告] " + msg)


# ---------------------------------------------------------------- 数据加载
def load_rank() -> dict:
    """rank 分片 -> 折叠键 (作者, 标题/词牌) -> 每引擎最大值。跳过 2019y 杂散文件。"""
    acc = collections.defaultdict(lambda: dict.fromkeys(ENGINES, 0))
    n = 0
    files = sorted(glob.glob(os.path.join(RANK_DIR, "poet", "*.json"))) + \
        sorted(glob.glob(os.path.join(RANK_DIR, "ci", "*.json")))
    if not files:
        sys.exit("未找到 rank 原始快照（rank/poet、rank/ci 已于 2026-10-16 清理）。\n"
                 "  重新评分需先恢复备份：poetry-pipeline/dist/backups/rank-raw-20261016.tar.gz\n"
                 "  仅做格式转换请用：python heat_score.py --flatten")
    for f in files:
        if "2019y" in os.path.basename(f):
            continue
        with open(f, encoding="utf-8") as fh:
            for r in json.load(fh):
                n += 1
                key = (fold(r.get("author", "")),
                       fold(r.get("title") or r.get("rhythmic") or ""))
                cell = acc[key]
                for e in ENGINES:
                    v = r.get(e, 0) or 0
                    if isinstance(v, int) and v > cell[e]:
                        cell[e] = v
    print("  rank 记录 %d 条，去重键 %d 个" % (n, len(acc)))
    return acc


def parse_id(pid: int):
    return (
        {0: "unknown", 1: "preqin", 2: "han", 3: "wudai", 4: "tang", 5: "song",
         6: "yuan", 7: "ming", 8: "qing", 9: "modern", 10: "weijin",
         11: "nanbeichao", 12: "sui", 13: "jin", 15: "custom"}[(pid >> 27) & 0xF],
        {0: "poem", 1: "ci", 2: "qu", 3: "prose", 4: "classic", 5: "fu",
         6: "article"}[(pid >> 24) & 0x7],
    )


def load_poems(con) -> list:
    """三集合全部诗词：uid/id/作者/标题/词牌/朝代/体裁/extras 六标志。"""
    q = """
    SELECT p.id, p.uid, a.name, p.title, r.name, s.name,
           COALESCE(e.translation,'')<>'', COALESCE(e.notes,'')<>'',
           COALESCE(e.appreciation,'')<>'',
           COALESCE(e.introduction,'')<>'' OR COALESCE(e.creative_background,'')<>'',
           COALESCE(e.images,'')<>''
    FROM poems p
    JOIN sources s ON s.id = p.src_id
    LEFT JOIN authors a ON a.uid = p.author_uid
    LEFT JOIN rhythmics r ON r.id = p.rhythmic_id
    LEFT JOIN poem_extras e ON e.uid = p.uid
    WHERE s.name IN (%s)
    """ % ",".join("?" * len(SCORED_COLLS))
    rows = con.execute(q, SCORED_COLLS).fetchall()
    print("  主库三集合诗词 %d 条" % len(rows))
    return rows


# ---------------------------------------------------------------- 评分
def evaluate(eng: dict, dynasty: str, ex: dict) -> dict:
    logs = [math.log10(1 + eng.get(e, 0)) for e in ENGINES]
    agg = sum(logs) / len(logs)
    cn = 0.6 * math.log10(1 + eng.get("baidu", 0)) + \
        0.4 * math.log10(1 + eng.get("so360", 0))
    intl = 1.0 if (eng.get("google", 0) >= 1_000_000
                   and eng.get("bing_en", 0) >= 100_000) else 0.0

    breadth = min(5.0, tier(agg) + 0.5 * intl)
    now = tier(cn)
    span = min(5.0, SPAN_BASE.get(dynasty, 2.5) + (0.5 if (ex["tr"] and ex["ap"]) else 0.0))
    depth = min(5.0, 1.5 + 0.75 * ex["tr"] + 0.75 * ex["nt"] + 1.0 * ex["ap"]
                + 0.5 * ex["in"] + 0.5 * ex["im"])
    composite = max(1, min(100, round(
        20 * (WEIGHTS["breadth"] * breadth + WEIGHTS["span"] * span
              + WEIGHTS["depth"] * depth + WEIGHTS["now"] * now))))
    stars = next(s for th, s in STAR_OF if composite >= th)
    return {
        "b": round(breadth, 2), "s": round(span, 2),
        "p": round(depth, 2), "n": round(now, 2),
        "c": composite, "st": stars,
    }


def build_scores(rank: dict, poems: list):
    """返回 {uid: record}；未命中 rank 键的诗不产分（视为无效评分）。"""
    out = {}
    for pid, uid, aname, title, rname, coll, tr, nt, ap, intro, im in poems:
        dynasty, kind = parse_id(pid)
        key = (fold(aname or ""), fold(rname or "")) if coll == "song-ci" \
            else (fold(aname or ""), fold(title or ""))
        eng = rank.get(key)
        if not eng:
            continue
        ex = {"tr": tr, "nt": nt, "ap": ap, "in": intro, "im": im}
        ev = evaluate(eng, dynasty, ex)
        out[uid] = {
            "t": title or "", "a": aname or "",
            "d": DYN_LABEL.get(dynasty, dynasty), "k": KIND_LABEL.get(kind, kind),
            "dm": {"b": ev["b"], "s": ev["s"], "p": ev["p"], "n": ev["n"]},
            "c": ev["c"], "st": ev["st"], "cm": STAR_COMMENT[ev["st"]],
            "src": {e: eng.get(e, 0) for e in ENGINES},
        }
    return out


# ---------------------------------------------------------------- 模式
def report(scores: dict, con):
    hist = collections.Counter(v["st"] for v in scores.values())
    print("\n星级分布：")
    for s in (5, 4, 3, 2, 1):
        print("  %s星 %7d 条 (%.1f%%)" % (s, hist[s], 100.0 * hist[s] / len(scores)))
    print("\nTop 20 综合热度：")
    top = sorted(scores.items(), key=lambda kv: -kv[1]["c"])[:20]
    for uid, v in top:
        print("  %3d %s %s《%s》 b=%.1f s=%.1f p=%.1f n=%.1f"
              % (v["c"], v["d"], v["a"], v["t"], v["dm"]["b"], v["dm"]["s"],
                 v["dm"]["p"], v["dm"]["n"]))
    # 与旧分对照：旧 Top10 在新体系下的位置
    print("\n旧热度 Top10 在新体系下的综合分：")
    old = con.execute(
        "SELECT p.uid, p.title, p.score FROM poems p JOIN sources s ON s.id=p.src_id "
        "WHERE s.name IN (%s) ORDER BY p.score DESC LIMIT 10" % ",".join("?" * 3),
        SCORED_COLLS).fetchall()
    for uid, title, sc in old:
        v = scores.get(uid)
        print("  旧%3d 《%s》 -> 新%s" % (sc, title, ("%d (%s星)" % (v["c"], v["st"])) if v else "未命中"))


# ---------------------------------------------------------------- 导出（扁平格式）
def _atomic_write(path: str, write_fn):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        write_fn(f)
    os.replace(tmp, path)


def write_flat_scores(path: str, flat: dict):
    """扁平 {uid: score}：uid 字典序、每行一条——diff 友好，按 uid O(1) 查询。"""

    def _w(f):
        f.write("{\n")
        f.write(",\n".join("%s: %d" % (json.dumps(k, ensure_ascii=False), v)
                           for k, v in sorted(flat.items())))
        f.write("\n}\n")

    _atomic_write(path, _w)


def write_json_file(path: str, obj):
    _atomic_write(path, lambda f: json.dump(obj, f, ensure_ascii=False, indent=1))


def export_json(scores: dict, details: bool = False) -> str:
    """扁平导出（2026-10-16 起）：heat-scores.json={uid: 综合分}，规则写 heat-meta.json。

    details=True 时另存富明细 heat-scores-details.json（四维/星级/评语/引擎证据）。
    扁平文件是权威产物；富明细按需归档，默认不写。
    """
    flat = {uid: v["c"] for uid, v in scores.items()}
    write_flat_scores(OUT, flat)
    print("  已写出扁平 %s（%.1f MB，%d 条）"
          % (OUT, os.path.getsize(OUT) / 1048576, len(flat)))

    meta = dict(META)
    meta["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["n_scored"] = len(flat)
    meta["scores_file"] = "heat-scores.json"
    meta["format"] = ("扁平 JSON：{uid: 综合热度分(1-100 整数)}，uid 字典序、每行一条，"
                      "便于按 uid O(1) 读写")
    meta["details_file"] = None
    if details:
        write_json_file(DETAILS_OUT, scores)
        meta["details_file"] = "heat-scores-details.json"
        print("  已写出富明细 %s（%.1f MB）"
              % (DETAILS_OUT, os.path.getsize(DETAILS_OUT) / 1048576))
    write_json_file(META_OUT, meta)
    print("  已写出 %s（规则/权重/阈值/可信度说明）" % META_OUT)
    return OUT


def flatten_existing():
    """把旧嵌套 heat-scores.json（{meta, scores:{uid:{...}}}）转为扁平 {uid: score}。

    - 不重算评分、不读 rank 原始快照（已清理），只做格式转换；
    - 旧富明细（四维/星级/评语/引擎证据）归档到 dist/backups/，不删除；
    - meta 拆到 rank/heat-meta.json，追加 flattened_at 与归档指针；
    - 转换前校验：综合分为 1-100 整数、uid 无重复、条数与 meta.n_scored 一致。
    """
    if not os.path.exists(OUT):
        sys.exit("找不到 %s" % OUT)
    with open(OUT, encoding="utf-8") as f:
        old = json.load(f)
    if not (isinstance(old, dict) and "scores" in old):
        print("%s 已是扁平格式，无需转换" % OUT)
        return

    scores = old["scores"]
    flat = {}
    for uid, rec in scores.items():
        c = rec.get("c") if isinstance(rec, dict) else None
        if not isinstance(c, int) or isinstance(c, bool) or not (1 <= c <= 100):
            sys.exit("uid %s 的综合分异常：%r" % (uid, c))
        flat[uid] = c
    if len(flat) != len(scores):
        sys.exit("uid 存在重复，终止转换")
    meta = dict(old.get("meta") or META)
    n_meta = meta.get("n_scored")
    if n_meta is not None and n_meta != len(flat):
        warn("meta.n_scored=%s 与实际条数 %d 不一致" % (n_meta, len(flat)))

    bakdir = os.path.join(HERE, "dist", "backups")
    os.makedirs(bakdir, exist_ok=True)
    archive = os.path.join(bakdir, "heat-scores-details-%s.json"
                           % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(OUT, archive)
    print("  旧富明细归档 -> %s（%.1f MB）"
          % (archive, os.path.getsize(archive) / 1048576))

    write_flat_scores(OUT, flat)
    print("  已写出扁平 %s（%.1f MB，%d 条）"
          % (OUT, os.path.getsize(OUT) / 1048576, len(flat)))

    meta["name"] = "heat-meta"
    meta["scores_file"] = "heat-scores.json"
    meta["format"] = ("扁平 JSON：{uid: 综合热度分(1-100 整数)}，uid 字典序、每行一条，"
                      "便于按 uid O(1) 读写；四维分/星级/评语/引擎证据见归档 details 文件")
    meta["flattened_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["details_archive"] = os.path.relpath(archive, ROOT).replace("\\", "/")
    write_json_file(META_OUT, meta)
    print("  已写出 %s（规则/权重/阈值/可信度说明）" % META_OUT)


def apply_db(scores: dict):
    if not os.path.exists(DB):
        sys.exit("找不到主库 %s" % DB)
    bak = DB + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    print("  备份主库 -> %s（%.1f MB）" % (bak, os.path.getsize(DB) / 1048576))
    shutil.copy2(DB, bak)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys=ON")
    old = con.execute("SELECT COUNT(*) FROM poems WHERE score>0").fetchone()[0]
    with con:
        # 旧分全部清零（过期数据不留），再写入新分
        con.execute("UPDATE poems SET score=0 WHERE score>0")
        con.executemany("UPDATE poems SET score=? WHERE uid=?",
                        [(v["c"], uid) for uid, v in scores.items()])
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for k, v in (
            ("heat_scored_at", now),
            ("heat_rubric_version", RUBRIC_VERSION),
            ("heat_scored_count", str(len(scores))),
            ("heat_prev_score_zeroed", str(old)),
            ("heat_note", "score 现为四维网络热度综合分 1-100（提示词星级模型），"
                          "历史值为五引擎 log 量化 0-255，已过期清零；"
                          "星级阈值 90/75/55/35，规则见 rank/heat-meta.json"),
        ):
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    new = con.execute("SELECT COUNT(*) FROM poems WHERE score>0").fetchone()[0]
    print("  poems.score：旧 %d 条非零 -> 新 %d 条非零（清零 %d 条未获新评分）"
          % (old, new, old - new))
    print("  Top5 新热度：")
    for t, c in con.execute("SELECT title,score FROM poems ORDER BY score DESC LIMIT 5"):
        print("    %3d 《%s》" % (c, t))
    con.close()
    return bak


def main():
    ap = argparse.ArgumentParser(description="按四维热度模型重评诗词并落库")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--flatten", action="store_true",
                    help="把旧嵌套 heat-scores.json 转为扁平 {uid: score}（不重算）")
    ap.add_argument("--details", action="store_true",
                    help="导出时另存富明细 heat-scores-details.json")
    args = ap.parse_args()
    if not (args.dry_run or args.export or args.apply or args.flatten):
        ap.error("必须指定 --dry-run / --export / --apply / --flatten 之一")

    if args.flatten:
        flatten_existing()
        return

    print("=== 1. 读取 rank 快照 ===")
    rank = load_rank()
    print("=== 2. 读取主库三集合 ===")
    con = sqlite3.connect(DB)
    poems = load_poems(con)
    print("=== 3. 四维评分 ===")
    scores = build_scores(rank, poems)
    n_miss = len(poems) - len(scores)
    print("  有效评分 %d 条；未命中 rank 键 %d 条（score 保持 0）" % (len(scores), n_miss))

    if args.dry_run:
        report(scores, con)
    if args.export or args.apply:
        print("=== 4. 导出 JSON ===")
        export_json(scores, details=args.details)
    if args.apply:
        print("=== 5. 更新主库 ===")
        apply_db(scores)
    con.close()
    print("完成")


if __name__ == "__main__":
    main()
