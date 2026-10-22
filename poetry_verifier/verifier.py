# -*- coding: utf-8 -*-
"""核对与决策层：跨源比对、缺失/错误/冲突识别、可应用的修改决策。

设计要点（程枢 C4/C5/C6）：
- 缺失/存疑字（□、[..]）做“填空式”精准补全，而非整段替换，降低误改风险。
- 标题/作者/正文的“冲突”（非占位符差异）默认不自动覆盖，仅标注待人工复核。
- 每个修改都带置信度；低于阈值的修改不自动写入。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import normalize as N
from .schemas import PoemRecord


# ----------------------- 占位符精准补全 -----------------------
def fill_line(local: str, ref: str) -> Tuple[str, bool]:
    """把 local 中的占位符（□ / [..]）按 ref 对应位置的字补全。

    算法：以 local 的非占位符片段为锚点，在 ref（转繁体、去标点）中按顺序定位，
    取锚点之间的字回填。无法可靠定位时返回原串（绝不臆造）。
    """
    parts = re.split(r"(\[[^\[\]]{0,8}\]|[□■◼○〇])", local)
    gaps = parts[1::2]
    if not gaps:
        return local, False
    lits = parts[0::2]
    ref_s = N.strip_punct(N.to_traditional(ref))
    fills = []
    pos = 0
    ok = True
    for j in range(len(gaps)):
        lit_a = N.strip_punct(lits[j])
        lit_b = N.strip_punct(lits[j + 1])
        i_a = ref_s.find(lit_a, pos)
        if i_a < 0:
            ok = False
            break
        pos = i_a + len(lit_a)
        if lit_b == "":
            # 行尾占位符（其后无锚点）：取参考串剩余部分回填
            fills.append(ref_s[pos:])
        else:
            i_b = ref_s.find(lit_b, pos)
            if i_b < 0:
                ok = False
                break
            fills.append(ref_s[pos:i_b])
            pos = i_b + len(lit_b)
    if not ok or len(fills) != len(gaps):
        return local, False
    out = []
    gi = 0
    for k, seg in enumerate(parts):
        if k % 2 == 0:
            out.append(seg)
        else:
            out.append(fills[gi])
            gi += 1
    filled = "".join(out)
    return filled, (filled != local and not N.has_placeholder(filled))


def _first_substantial_line(local_lines: List[str]) -> Optional[str]:
    """取本地正文第一条『可比对』且长度≥4 的行（跳过空行/过短行）。"""
    for ll in local_lines:
        cl = N.to_comparable(ll).strip()
        if len(cl) >= 4:
            return cl
    return None


def _looks_like_different_poem(local_lines: List[str], ref_lines: List[str]) -> bool:
    """判断本地正文与权威正文是否疑似『不同的作品』（而非同一作品的版本异文）。

    信号：本地首句（≥4 字）未出现在权威正文中，且整体相似度很低。
    同一作品的不同版本首句几乎必然相同；同作者/同词牌下匹配到另一首词时首句完全不同。
    用于拦截『同詞牌+同作者误配到另一首词』导致的灾难性覆盖。
    """
    first = _first_substantial_line(local_lines)
    if not first:
        return False
    rt = N.to_comparable("\n".join(ref_lines))
    if first in rt:
        return False  # 首句命中 → 同一作品
    # 首句不在权威正文中 → 用整体相似度进一步确认
    import difflib
    lt = N.to_comparable("\n".join(local_lines))
    if not lt:
        return False
    ratio = difflib.SequenceMatcher(None, lt, rt).ratio()
    return ratio < 0.6


def _has_foreign_prefix(local_lines: List[str], ref_lines: List[str]) -> bool:
    """本地首句在权威正文中位置过于靠后（其前有 ≥8 字实质内容）→ 疑为多首合页。

    与 _looks_like_different_poem 互补：后者管“首句完全不在权威正文中”，
    前者管“首句虽在、但前面还挂着别的作品”（古文岛同词牌多首合页的典型形态）。
    检索层已做片段截取，这里是防御其他检索路径（如 search_api）的兜底。
    """
    first = _first_substantial_line(local_lines)
    if not first:
        return False
    rt = N.to_comparable("\n".join(ref_lines))
    pos = rt.find(first)
    if pos < 0:
        return False
    return len(rt[:pos]) >= 8


def _best_match_line(local: str, ref_lines: List[str]) -> Optional[str]:
    lc = N.to_comparable(local)
    if not lc:
        return None
    best, best_r = None, -1.0
    import difflib
    for rl in ref_lines:
        r = difflib.SequenceMatcher(None, lc, N.to_comparable(rl)).ratio()
        if r > best_r:
            best, best_r = rl, r
    return best


def fill_placeholders(local_lines: List[str], ref_lines: List[str]) -> Tuple[List[str], bool]:
    ref_trad = [N.to_traditional(r) for r in ref_lines]
    new_lines = []
    any_filled = False
    for ll in local_lines:
        if not N.has_placeholder(ll):
            new_lines.append(ll)
            continue
        ref = _best_match_line(ll, ref_trad)
        if not ref:
            new_lines.append(ll)
            continue
        filled, improved = fill_line(ll, ref)
        if improved:
            new_lines.append(filled)
            any_filled = True
        else:
            new_lines.append(ll)
    return new_lines, any_filled


# ----------------------- 比对与决策 -----------------------
def _dyn_norm(d: Optional[str]) -> Optional[str]:
    if not d:
        return None
    return d.replace("代", "").replace("朝", "").strip() or None


def decide(record: PoemRecord, ref, settings) -> dict:
    """返回决策结果：{status, changes:[...], notes}。

    每个 change: {field, old, new, reason, confidence, applied, needs_review}
    applied 由本函数依据 settings 直接判定；orchestrator 据此落盘。
    """
    changes: List[dict] = []
    notes = []

    # ---- 正文 ----
    if record.content_key:
        local_comp = N.comparable_lines(record.content_lines)
        ref_comp = N.comparable_lines(ref.content_lines)
        if local_comp == ref_comp:
            pass  # 完全一致
        elif _looks_like_different_poem(record.content_lines, ref.content_lines) or \
                _has_foreign_prefix(record.content_lines, ref.content_lines):
            # 错配拦截：疑似匹配到同作者/同词牌的另一首作品，或权威页面为多首合页。
            # 直接跳过正文覆盖（返回独立状态），避免任何裁判把整首正确词覆盖成另一首。
            notes.append(
                "参考疑似错配：本地正文首句未出现在权威正文中，或权威正文在首句前"
                "含有其他作品内容；已跳过正文覆盖以防误改（建议人工核对具体篇目）"
            )
            return {"status": "reference_mismatch", "changes": [], "notes": notes}
        elif settings.verify.fill_placeholders and N.has_placeholder(record.content_text):
            new_lines, filled = fill_placeholders(record.content_lines, ref.content_lines)
            if filled:
                old = record.content_lines
                fully = not N.has_placeholder("\n".join(new_lines))
                conf = 0.9 if fully else 0.6
                applied = settings.verify.fill_placeholders and conf >= settings.verify.confidence_threshold
                changes.append({
                    "field": record.content_key,
                    "old": old,
                    "new": new_lines,
                    "reason": "补全缺失/存疑字（□/[..] → 权威来源原文）",
                    "confidence": conf,
                    "applied": applied,
                    "needs_review": (not applied),
                })
            else:
                changes.append(_conflict(record.content_key, record.content_lines,
                                         ref.content_lines, "正文与权威来源不一致，无法可靠补全", 0.5, settings))
        else:
            changes.append(_conflict(record.content_key, record.content_lines,
                                     ref.content_lines, "正文与权威来源不一致", 0.5, settings))

    # ---- 标题 ----
    if record.title is not None and ref.title:
        if N.to_comparable(record.title) != N.to_comparable(ref.title):
            changes.append(_conflict("title", record.title, ref.title,
                                     "标题与权威来源不一致", 0.6, settings))
    elif record.title is None and ref.title:
        changes.append(_completion("title", None, ref.title, "补全缺失标题", 0.85, settings))

    # ---- 作者 ----
    if record.author is not None and ref.author:
        if N.to_comparable(record.author) != N.to_comparable(ref.author):
            changes.append(_conflict("author", record.author, ref.author,
                                     "作者与权威来源不一致", 0.6, settings))
    elif record.author is None and ref.author:
        changes.append(_completion("author", None, ref.author, "补全缺失作者", 0.85, settings))

    # ---- 朝代 ----
    ref_dyn = _dyn_norm(ref.dynasty)
    has_dyn = ("dynasty" in record.obj)
    if has_dyn:
        local_dyn = _dyn_norm(record.obj.get("dynasty"))
        if ref_dyn and local_dyn and local_dyn != ref_dyn:
            changes.append(_conflict("dynasty", record.obj.get("dynasty"), ref.dyn or ref_dynasty,
                                     "朝代与权威来源不一致", 0.7, settings))
    else:
        # 缺失朝代：优先用文集推断（folder），其次用来源
        expected = record.dynasty_derived or ref_dyn
        if expected:
            reason = "补全缺失朝代（依据文集/权威来源）"
            changes.append(_completion("dynasty", None, expected, reason, 0.9, settings))

    # ---- 汇总状态 ----
    applied_any = any(c["applied"] for c in changes)
    review_any = any(c["needs_review"] and not c["applied"] for c in changes)
    if review_any:
        status = "conflict_needs_review"
    elif applied_any:
        status = "corrected"
    else:
        status = "verified_ok"

    return {"status": status, "changes": changes, "notes": notes}


def _conflict(field, old, new, reason, conf, settings) -> dict:
    applied = settings.verify.overwrite_conflicts and conf >= settings.verify.confidence_threshold
    return {
        "field": field, "old": old, "new": new, "reason": reason,
        "confidence": conf, "applied": applied, "needs_review": (not applied),
    }


def _completion(field, old, new, reason, conf, settings) -> dict:
    # 权威来源（如 gushiwen）标题/作者常带繁体；语料库整体已简体化（commit a6f08e9），
    # 补全字段需转简体以保持全库一致，避免把繁体标题写回简体语料。
    if isinstance(new, str):
        new = N.to_simplified(new)
    applied = settings.verify.add_missing_fields and conf >= settings.verify.confidence_threshold
    return {
        "field": field, "old": old, "new": new, "reason": reason,
        "confidence": conf, "applied": applied, "needs_review": (not applied),
    }
