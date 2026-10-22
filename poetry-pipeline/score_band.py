"""分数分档（score banding）：把 0-100 的整数分按「第一档 90-100，其余每两个连续分数一档」归档。

规则（2026-10-17 更正）
------------------------
早先规则为「100-99 第1档、98-97 第2档……」；按更正后的标准，**第一档为 90-100 分**：

    第 1 档：100 ~ 90      （顶级带，含 11 个值）
    第 2 档： 89 ~ 88
    第 3 档： 87 ~ 86
    第 4 档： 85 ~ 84
    ...
    第46档：  1 ~  0       （末档，含 0）

各档区间的起止边界与包含关系
--------------------------
- 第 1 档是**闭区间** [90, 100]，上界和下界**都包含**在该档内：
  100 与 90 同属第 1 档；89 与 90 是第 1、2 档的分界值，分属两档。
- 第 k 档（k≥2）是闭区间 [92-2k, 93-2k]：第 2 档 = [88, 89]，
  第 3 档 = [86, 87]，…… 第 46 档 = [0, 1]。
- 相邻档**不重叠、无空隙**：第 1 档下界（90）恰比第 2 档上界（89）大 1，
  第 k 档下界比第 k+1 档上界大 1，任意整数分有且只有一档。
- 输入限定为整数，因此不存在「89.5 归哪档」的边界歧义。
- 0-100 共 101 个整数：第 1 档占 11 个，其余 90 个两两成档得 45 档，
  合计 **46 档**。
- 档号越小分数越高：第 1 档最高，第 46 档最低。

等价公式
--------
    tier = 1                      if score >= 90
    tier = 2 + (89 - score) // 2   otherwise

输入域与异常
------------
- 仅接受**整数**（int，或任何实现 __index__ 的整数型，如 numpy 整数）；
  整数值的浮点（如 98.0）、字符串、None 等一律拒绝——契约是类型严格的整数。
- 布尔值显式拒绝：True 若按 int 会被静当作 1 分（第 46 档），属于易错点。
- 超出 0-100 -> ScoreOutOfRangeError；非整数/非法类型 -> ScoreTypeError。
  二者都是 ScoreBandError（ValueError 子类），调用方可统一捕获 ScoreBandError
  或 ValueError；ScoreTypeError 同时是 TypeError，按类型错误捕获也成立。

适用范围
--------
本库 poems.score / authors.score 均为 1-100 的综合热度分；诗人分级系统
（rank/poet-tiers.json）34 位顶级诗人的分值 92-100，按本规则全部落在
第 1 分数带（90-100）内。历史上的 0-255 五引擎量化分已不在域内
（会抛 ScoreOutOfRangeError）。
"""

from __future__ import annotations

import operator

__all__ = [
    "MIN_SCORE", "MAX_SCORE", "MIN_TIER", "MAX_TIER", "TOP_TIER_MIN",
    "ScoreBandError", "ScoreTypeError", "ScoreOutOfRangeError",
    "score_to_tier", "tier_interval", "tier_label", "iter_tiers",
]

MIN_SCORE = 0
MAX_SCORE = 100
MIN_TIER = 1
# 第一档（顶级带）的下界：90-100 同属第 1 档
TOP_TIER_MIN = 90
# 第 1 档占 11 个值，其余 0-89 两两成档得 45 档，合计 46 档
MAX_TIER = 1 + (TOP_TIER_MIN - MIN_SCORE) // 2  # = 46


class ScoreBandError(ValueError):
    """分数分档输入异常的基类。

    继承 ValueError：调用方既可以精确捕获本类，也可以按通用的
    ValueError 捕获，不影响既有 except ValueError 的代码。
    """


class ScoreTypeError(ScoreBandError, TypeError):
    """分数不是整数 / 类型非法（浮点、字符串、None、bool、容器等）。"""


class ScoreOutOfRangeError(ScoreBandError):
    """整数分数或档位超出允许范围。"""


def _coerce_int(value, what: str) -> int:
    """把 value 约束为真正的整数；bool 与一切非整数型都拒绝。

    用 operator.index 而不是 isinstance(value, int)：前者只接受真正具备
    整数语义的类型（int、numpy 整数、IntEnum 等），float/str/None 全部抛
    TypeError；后者放不过 bool（bool 是 int 的子类），需单独排除。
    """
    if isinstance(value, bool):
        raise ScoreTypeError("%s必须是整数，拒绝布尔值 %r（True 会被误当作 1）"
                             % (what, value))
    try:
        return operator.index(value)
    except TypeError:
        raise ScoreTypeError("%s必须是整数，收到 %s 值 %r"
                             % (what, type(value).__name__, value)) from None


def score_to_tier(score) -> int:
    """整数分数 -> 档位。90-100 为第 1 档，每低 2 分档号 +1。

    公式::

        tier = 1                      if score >= 90
        tier = 2 + (89 - score) // 2   otherwise

    >>> score_to_tier(100)
    1
    >>> score_to_tier(90)
    1
    >>> score_to_tier(89)
    2
    >>> score_to_tier(0)
    46
    """
    value = _coerce_int(score, "分数")
    if not (MIN_SCORE <= value <= MAX_SCORE):
        raise ScoreOutOfRangeError("分数 %d 超出允许范围 %d-%d"
                                   % (value, MIN_SCORE, MAX_SCORE))
    if value >= TOP_TIER_MIN:
        return 1
    return 2 + (TOP_TIER_MIN - 1 - value) // 2


def tier_interval(tier) -> tuple:
    """档位 -> 闭区间 ``(下界, 上界)``，两端都属于该档。

    >>> tier_interval(1)
    (90, 100)
    >>> tier_interval(2)
    (88, 89)
    >>> tier_interval(46)
    (0, 1)
    """
    t = _coerce_int(tier, "档位")
    if not (MIN_TIER <= t <= MAX_TIER):
        raise ScoreOutOfRangeError("档位 %d 超出允许范围 %d-%d"
                                   % (t, MIN_TIER, MAX_TIER))
    if t == 1:
        return (TOP_TIER_MIN, MAX_SCORE)
    low = TOP_TIER_MIN - 2 * (t - 1)     # 第2档=88，第3档=86，……第46档=0
    high = low + 1
    return (max(MIN_SCORE, low), high)


def tier_label(tier) -> str:
    """档位 -> 人类可读标签，如 ``第1档（90~100）``。"""
    low, high = tier_interval(tier)
    return "第%d档（%d~%d）" % (tier, low, high)


def iter_tiers():
    """遍历全部档位，产出 ``(档号, 下界, 上界)``，档号从小到大。"""
    for t in range(MIN_TIER, MAX_TIER + 1):
        low, high = tier_interval(t)
        yield t, low, high


if __name__ == "__main__":
    print("分数分档表（第一档 90-100，其余每档为闭区间、两端均包含；档号越小分数越高）")
    print("-" * 46)
    for t, low, high in iter_tiers():
        print("  第%-2d档: %3d ~ %-3d" % (t, low, high))
    print("-" * 46)
    print("示例：")
    for s in (100, 90, 89, 88, 86, 75, 61, 10, 2, 1, 0):
        print("  score_to_tier(%3d) = %d" % (s, score_to_tier(s)))
