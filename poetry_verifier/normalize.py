# -*- coding: utf-8 -*-
"""文本归一化：繁简转换、标点/空白清理，用于跨源比对。

注意：本地语料为繁体（全唐诗/宋词等），古文岛为简体。比对时统一转繁体，
避免把“繁简差异”误判为“内容冲突”。
"""
from __future__ import annotations

import re
import unicodedata

try:
    import zhconv  # 项目已固定依赖 zhconv==1.4.3
    _HAS_ZHCONV = True
except Exception:  # pragma: no cover
    _HAS_ZHCONV = False


def to_traditional(text: str) -> str:
    if not text:
        return text
    if _HAS_ZHCONV:
        try:
            return zhconv.convert(text, "zh-hant")
        except Exception:
            return text
    return text


def to_simplified(text: str) -> str:
    if not text:
        return text
    if _HAS_ZHCONV:
        try:
            return zhconv.convert(text, "zh-hans")
        except Exception:
            return text
    return text


# 需要剔除的标点（中英文），用于“可比对”字符串
_PUNCT = (
    " ，。、？！；：“”‘’「」『』（）()《》〈〉·—…~～"
    "．.,;:!?\"'‘’“”[]{}<>|/"
)
_PUNCT_RE = re.compile("[" + re.escape(_PUNCT) + "]")
_WS_RE = re.compile(r"\s+")


def strip_punct(text: str) -> str:
    return _PUNCT_RE.sub("", text or "")


def collapse_ws(text: str) -> str:
    return _WS_RE.sub("", text or "")


def to_comparable(text: str) -> str:
    """生成用于跨源比对的规范串：转繁体 → 去标点 → 去空白。"""
    if not text:
        return ""
    t = to_traditional(text)
    t = strip_punct(t)
    t = collapse_ws(t)
    return t


def comparable_lines(lines) -> str:
    """把分段正文拼成一个可比对串。"""
    if lines is None:
        return ""
    if isinstance(lines, str):
        return to_comparable(lines)
    return to_comparable("".join(lines))


# 正文缺失/存疑占位符
_PLACEHOLDER_RE = re.compile(r"[□■◼○〇]|\[[^\[\]]{0,8}\]")


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text or ""))


def normalize_nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def split_verses(text: str) -> list:
    """将一段诗文拆成“句/段”列表，尽量贴近本地 paragraphs 的逐句结构。"""
    if not text:
        return []
    # 先按显式换行/分节拆
    parts = re.split(r"[\n\r]+|</?p>", text)
    out = []
    # 句末标点切分（。！？；），让长句也能逐句成段
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 保护引号内的内容不被切断
        segments = re.split(r"(?<=[。！？；])", p)
        for seg in segments:
            seg = seg.strip()
            if seg:
                out.append(seg)
    return out


if __name__ == "__main__":
    print(to_comparable("床前明月光，疑是地上霜。"))
    print(to_comparable("床前明月光，疑是地上霜。") == to_comparable("床前明月光疑是地上霜"))
    print(split_verses("床前明月光，疑是地上霜。举头望明月，低头思故乡。"))
