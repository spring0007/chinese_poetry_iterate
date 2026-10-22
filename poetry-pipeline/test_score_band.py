"""score_band 的单元测试：覆盖典型边界值、区间划分性质与全部异常路径。

运行：python -m unittest test_score_band -v
"""

import unittest
from decimal import Decimal

from score_band import (
    MAX_TIER,
    MIN_TIER,
    TOP_TIER_MIN,
    ScoreBandError,
    ScoreOutOfRangeError,
    ScoreTypeError,
    iter_tiers,
    score_to_tier,
    tier_interval,
    tier_label,
)


class TestTierBoundaries(unittest.TestCase):
    """档位边界：第一档 90-100，其余每档两个连续整数，90/89 是第 1、2 档的分界。"""

    def test_first_tier_is_90_to_100(self):
        # 更正后标准：第一档 = 90-100（闭区间，11 个值）
        for s in range(90, 101):
            self.assertEqual(score_to_tier(s), 1,
                             "%d 应属第 1 档（90-100）" % s)

    def test_next_tiers_closed_intervals(self):
        # 第 2/3/4 档各含两个连续整数，两端都算
        self.assertEqual(score_to_tier(89), 2)
        self.assertEqual(score_to_tier(88), 2)
        self.assertEqual(score_to_tier(87), 3)
        self.assertEqual(score_to_tier(86), 3)
        self.assertEqual(score_to_tier(85), 4)
        self.assertEqual(score_to_tier(84), 4)

    def test_boundary_values_belong_to_adjacent_tiers(self):
        # 跨档相邻值必须分属不同档，不允许重叠
        self.assertNotEqual(score_to_tier(90), score_to_tier(89))
        self.assertNotEqual(score_to_tier(88), score_to_tier(87))
        self.assertNotEqual(score_to_tier(4), score_to_tier(3))

    def test_last_tiers(self):
        # 末两档：{2,3} 为第 45 档，{0,1} 为第 46 档
        self.assertEqual(score_to_tier(3), 45)
        self.assertEqual(score_to_tier(2), 45)
        self.assertEqual(score_to_tier(1), 46)
        self.assertEqual(score_to_tier(0), 46)
        self.assertEqual(MAX_TIER, 46)

    def test_pairing_rule(self):
        # 第一档（90-100）不参与两两配对；0-89 中偶数与比它大 1 的数同档
        for s in range(0, 89, 2):
            self.assertEqual(score_to_tier(s), score_to_tier(s + 1),
                             "%d 与 %d 应同档" % (s, s + 1))
        # 第一档内部全部同档
        tiers_in_top = {score_to_tier(s) for s in range(90, 101)}
        self.assertEqual(tiers_in_top, {1})

    def test_known_interval_table(self):
        self.assertEqual(tier_interval(1), (90, 100))
        self.assertEqual(tier_interval(2), (88, 89))
        self.assertEqual(tier_interval(3), (86, 87))
        self.assertEqual(tier_interval(4), (84, 85))
        self.assertEqual(tier_interval(5), (82, 83))
        self.assertEqual(tier_interval(23), (46, 47))
        self.assertEqual(tier_interval(45), (2, 3))
        self.assertEqual(tier_interval(46), (0, 1))

    def test_typical_scores(self):
        cases = {100: 1, 90: 1, 89: 2, 88: 2, 86: 3, 84: 4, 82: 5,
                 75: 9, 74: 9, 61: 16, 60: 16,
                 10: 41, 2: 45, 1: 46, 0: 46}
        for score, tier in cases.items():
            self.assertEqual(score_to_tier(score), tier,
                             "score=%d 应为第 %d 档" % (score, tier))

    def test_tier_label(self):
        self.assertEqual(tier_label(1), "第1档（90~100）")
        self.assertEqual(tier_label(2), "第2档（88~89）")
        self.assertEqual(tier_label(46), "第46档（0~1）")


class TestIntervalProperties(unittest.TestCase):
    """区间整体性质：全覆盖、不重叠、单调。"""

    def test_intervals_partition_domain(self):
        # 46 个闭区间恰好划分 0..100：无重叠、无空隙、全覆盖
        seen = {}
        for t in range(MIN_TIER, MAX_TIER + 1):
            low, high = tier_interval(t)
            self.assertLessEqual(low, high, "第%d档区间倒置" % t)
            for s in range(low, high + 1):
                if s in seen:
                    self.fail("分数 %d 同属第 %d 档与第 %d 档" % (s, seen[s], t))
                seen[s] = t
        self.assertEqual(sorted(seen), list(range(0, 101)))

    def test_first_tier_has_eleven_values(self):
        # 第一档 90-100 共 11 个值；其余每档恰好 2 个值
        sizes = {t: high - low + 1 for t, low, high in iter_tiers()}
        self.assertEqual(sizes[1], 11)
        for t in range(2, MAX_TIER + 1):
            self.assertEqual(sizes[t], 2, "第%d档应含 2 个值" % t)

    def test_exhaustive_roundtrip(self):
        # 任意分数：档位合法，且其区间包含该分数
        for s in range(0, 101):
            t = score_to_tier(s)
            self.assertTrue(MIN_TIER <= t <= MAX_TIER)
            low, high = tier_interval(t)
            self.assertLessEqual(low, s)
            self.assertLessEqual(s, high)

    def test_monotonic(self):
        # 分数越低档号越大（单调不降）；第一档内档号恒为 1
        prev = 0
        for s in range(100, -1, -1):
            t = score_to_tier(s)
            self.assertGreaterEqual(t, prev)
            prev = t
        # 0-88 范围内每降 2 分档号严格 +1
        for s in range(88, 0, -2):
            self.assertEqual(score_to_tier(s - 2), score_to_tier(s) + 1)

    def test_iter_tiers(self):
        tiers = list(iter_tiers())
        self.assertEqual(len(tiers), 46)
        self.assertEqual(tiers[0], (1, 90, 100))
        self.assertEqual(tiers[-1], (46, 0, 1))
        self.assertEqual(TOP_TIER_MIN, 90)


class TestOutOfRange(unittest.TestCase):
    """超出 0-100 的整数输入。"""

    def test_negative_and_above_100(self):
        for bad in (-1, -2, -100, 101, 102, 150, 255, 1000):
            with self.assertRaises(ScoreOutOfRangeError, msg="%d 应抛范围异常" % bad):
                score_to_tier(bad)

    def test_range_error_is_value_error(self):
        # 旧 0-255 引擎分不在域内；异常同时是 ScoreBandError / ValueError
        with self.assertRaises(ScoreBandError):
            score_to_tier(255)
        with self.assertRaises(ValueError):
            score_to_tier(-1)

    def test_boundary_values_ok(self):
        # 0 和 100 是合法端点，不抛异常
        self.assertEqual(score_to_tier(0), 46)
        self.assertEqual(score_to_tier(100), 1)

    def test_tier_out_of_range(self):
        for bad in (0, 47, 48, -1):
            with self.assertRaises(ScoreOutOfRangeError):
                tier_interval(bad)


class TestInvalidTypes(unittest.TestCase):
    """非整数与非法输入。"""

    def test_floats_rejected(self):
        # 含整数值浮点：契约是类型严格整数
        for bad in (98.5, 99.0, 100.0, 90.0, 0.0, -1.0):
            with self.assertRaises(ScoreTypeError, msg="%r 应抛类型异常" % bad):
                score_to_tier(bad)

    def test_non_numeric_rejected(self):
        for bad in ("98", "99.5", "", None, [], {}, object(), (1,), Decimal("98")):
            with self.assertRaises(ScoreTypeError, msg="%r 应抛类型异常" % bad):
                score_to_tier(bad)

    def test_bool_rejected(self):
        # True/False 若是 int 会被静当作 1/0 分，必须显式拒绝
        for bad in (True, False):
            with self.assertRaises(ScoreTypeError, msg="%r 应抛类型异常" % bad):
                score_to_tier(bad)

    def test_type_error_dual_inheritance(self):
        # ScoreTypeError 既可按 ScoreBandError/ValueError 捕获，也可按 TypeError 捕获
        self.assertTrue(issubclass(ScoreTypeError, ScoreBandError))
        self.assertTrue(issubclass(ScoreTypeError, TypeError))
        with self.assertRaises(TypeError):
            score_to_tier("98")

    def test_tier_invalid_types(self):
        for bad in (1.0, True, "1", None, [1]):
            with self.assertRaises(ScoreBandError):
                tier_interval(bad)


class TestExceptionHierarchy(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(ScoreBandError, ValueError))
        self.assertTrue(issubclass(ScoreOutOfRangeError, ScoreBandError))
        self.assertTrue(issubclass(ScoreTypeError, ScoreBandError))

    def test_error_messages_are_informative(self):
        with self.assertRaises(ScoreOutOfRangeError) as ctx:
            score_to_tier(101)
        self.assertIn("101", str(ctx.exception))
        self.assertIn("0-100", str(ctx.exception))
        with self.assertRaises(ScoreTypeError) as ctx:
            score_to_tier("98")
        self.assertIn("整数", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
