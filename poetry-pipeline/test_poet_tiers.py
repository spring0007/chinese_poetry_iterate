"""poet_tiers 的单元测试：分级结构、分数映射、名次规则、覆盖层与同步安全闸。

运行：python -m unittest test_poet_tiers -v
"""

import json
import os
import tempfile
import unittest

import poet_tiers as PT


EXPECTED_TIERS = {
    1: (("屈原", "李白", "杜甫", "苏轼"), 100),
    2: (("陶渊明", "曹植", "白居易", "李商隐", "辛弃疾", "李清照", "毛泽东"), 98),
    3: (("王维", "杜牧", "陆游", "李煜", "马致远"), 96),
    4: (("艾青", "穆旦", "胡适", "北岛", "海子", "顾城", "舒婷",
         "戴望舒", "卞之琳"), 94),
    5: (("王勃", "陈子昂", "孟浩然", "王昌龄", "刘禹锡",
         "郭沫若", "闻一多", "徐志摩", "何其芳"), 92),
}


class _Cur:
    """最小游标桩：包一层可迭代行。sqlite3 的行是元组，桩也必须给元组。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _StubCon:
    """DB 桩：回答 name_uid_map 的聚合查询与 apply_overrides_rich 的 dynasty 查询。

    with_rows 之外的诗人视作「库中无作者行」，桩不返回它们的行。
    """

    def __init__(self, dynasties=None, with_rows=None):
        self._d = dynasties or {}
        self._rows = with_rows if with_rows is not None else set(PT.TIER_POETS[1])

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        if s.startswith("select a.name, a.uid, count(p.id)"):
            names = set(params) & self._rows
            return _Cur([(n, "u-" + n, 10) for n in sorted(names)])
        if s.startswith("select dynasty from authors where uid=?"):
            uid = params[0]
            return _Cur([(self._d.get(uid, "tang"),)] if uid in self._d else [])
        raise AssertionError("unexpected SQL: %s" % sql)


class _SyncStubCon:
    """sync_db 桩：模拟 authors 表 + 单事务提交。"""

    def __init__(self, authors):
        self.authors = dict(authors)
        self.meta = {}
        self._updates = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        if s.startswith("select uid, score from authors"):
            return _Cur(list(self.authors.items()))
        if s.startswith("update authors set score=0"):
            self.authors = {u: 0 for u in self.authors}
            return _Cur([])
        if s.startswith("update authors set score=? where uid=?"):
            self._updates.append(params)          # 每行是一个 (score, uid) 元组
            return _Cur([])
        if s.startswith("insert or replace into meta"):
            k, v = params                       # executemany 逐行下发，params 是单行 (k, v)
            self.meta[k] = v
            return _Cur([])
        if s.startswith("select score from authors where uid=?"):
            uid = params[0]
            return _Cur([(self.authors[uid],)] if uid in self.authors else [])
        raise AssertionError("unexpected SQL: %s" % sql)

    def executemany(self, sql, params):
        for p in params:
            self.execute(sql, p)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for score, uid in self._updates:      # 模拟提交
            self.authors[uid] = score
        self._updates = []
        return False


class TestStructure(unittest.TestCase):
    """分级数据结构：五档、34 位、分数与用户给定数据一致。"""

    def test_tiers_match_user_data(self):
        self.assertEqual(PT.TIER_POETS,
                         {t: names for t, (names, _s) in EXPECTED_TIERS.items()})
        self.assertEqual(PT.TIER_SCORES,
                         {t: s for t, (_n, s) in EXPECTED_TIERS.items()})

    def test_validate_passes(self):
        self.assertEqual(PT.validate(), 34)

    def test_all_poets_unique(self):
        seen = [n for names in PT.TIER_POETS.values() for n in names]
        self.assertEqual(len(seen), 34)
        self.assertEqual(len(set(seen)), 34)

    def test_tier_scores_strictly_decreasing_in_top_band(self):
        scores = [PT.TIER_SCORES[t] for t in sorted(PT.TIER_SCORES)]
        self.assertEqual(scores, [100, 98, 96, 94, 92])
        for s in scores:
            self.assertTrue(90 <= s <= 100, "%d 应在第一档分数带 90-100 内" % s)
        for a, b in zip(scores, scores[1:]):
            self.assertGreater(a, b)

    def test_scores_all_in_band_one(self):
        # 更正后标准：第一档分数带 = 90-100，34 位诗人全部在第 1 带
        from score_band import score_to_tier
        for name, _c in PT.iter_poets():
            self.assertEqual(score_to_tier(PT.get_score(name)), 1)

    def test_validate_rejects_bad_data(self):
        orig = PT.TIERS
        try:
            PT.TIERS = dict(orig)
            PT.TIER_POETS = {t: names for t, (names, _s) in PT.TIERS.items()}
            PT.TIER_SCORES = {t: s for t, (_n, s) in PT.TIERS.items()}
            # 1) 重复诗人
            PT.TIER_POETS[1] = ("屈原", "屈原")
            with self.assertRaises(PT.TierError):
                PT.validate()
            # 2) 空档
            PT.TIER_POETS[1] = ("屈原", "李白", "杜甫", "苏轼")
            PT.TIER_POETS[3] = ()
            with self.assertRaises(PT.TierError):
                PT.validate()
            # 3) 分数越出 90-100 带
            PT.TIER_POETS[3] = ("王维", "杜牧", "陆游", "李煜", "马致远")
            PT.TIER_SCORES[3] = 88
            with self.assertRaises(PT.TierError):
                PT.validate()
            # 4) 分数不随档位递减
            PT.TIER_SCORES[3] = 96
            PT.TIER_SCORES[4] = 97
            with self.assertRaises(PT.TierError):
                PT.validate()
        finally:
            PT.TIERS = orig
            PT.TIER_POETS = {t: names for t, (names, _s) in orig.items()}
            PT.TIER_SCORES = {t: s for t, (_n, s) in orig.items()}


class TestQueries(unittest.TestCase):
    """查询接口。"""

    def test_classify_each_poet(self):
        for t, (names, score) in EXPECTED_TIERS.items():
            for n in names:
                c = PT.classify(n)
                self.assertEqual(c["tier"], t)
                self.assertEqual(c["score"], score)
                self.assertEqual(c["score_band"], 1)

    def test_get_tier_and_score(self):
        self.assertEqual(PT.get_tier("屈原"), 1)
        self.assertEqual(PT.get_score("屈原"), 100)
        self.assertEqual(PT.get_tier("陶渊明"), 2)
        self.assertEqual(PT.get_score("陶渊明"), 98)
        self.assertEqual(PT.get_tier("王维"), 3)
        self.assertEqual(PT.get_score("艾青"), 94)
        self.assertEqual(PT.get_tier("王勃"), 5)
        self.assertEqual(PT.get_score("王勃"), 92)

    def test_unknown_poet(self):
        self.assertIsNone(PT.classify("李贺"))
        for fn in (PT.get_tier, PT.get_score):
            with self.assertRaises(PT.TierError):
                fn("李贺")

    def test_iter_poets_order(self):
        order = [n for n, _ in PT.iter_poets()]
        self.assertEqual(len(order), 34)
        # 档位 1->5，档内姓名升序
        expected = [n for t in sorted(EXPECTED_TIERS)
                    for n in sorted(EXPECTED_TIERS[t][0])]
        self.assertEqual(order, expected)

    def test_score_overrides(self):
        ovr = PT.score_overrides()
        self.assertEqual(len(ovr), 34)
        self.assertEqual(ovr["屈原"], 100)
        self.assertEqual(ovr["陶渊明"], 98)
        self.assertEqual(ovr["王维"], 96)
        self.assertEqual(ovr["艾青"], 94)
        self.assertEqual(ovr["王勃"], 92)


class TestRanking(unittest.TestCase):
    """标准竞赛排名（1224）与并列处理。"""

    def test_full_ranking(self):
        records = [{"id": None, "name": n, "tier": PT.get_tier(n),
                    "score": PT.get_score(n)} for n, _ in PT.iter_poets()]
        ranked = PT.rank_records(records)
        self.assertEqual(len(ranked), 34)
        # 各档名次：4/7/5/9/9 人 -> 1, 5, 12, 17, 26
        expect = {1: 1, 2: 5, 3: 12, 4: 17, 5: 26}
        for r in ranked:
            self.assertEqual(r["rank"], expect[r["tier"]],
                             "%s 名次应为 %d" % (r["name"], expect[r["tier"]]))
        # 排序：分数降序，同分姓名升序
        prev = None
        for r in ranked:
            if prev is not None:
                self.assertGreaterEqual(prev["score"], r["score"])
                if prev["score"] == r["score"]:
                    self.assertLess(prev["name"], r["name"])
            prev = r

    def test_ranking_order_and_tiebreak(self):
        records = [
            {"id": "z", "name": "苏轼", "score": 100},
            {"id": "a", "name": "杜甫", "score": 100},
            {"id": "m", "name": "陶渊明", "score": 98},
        ]
        ranked = PT.rank_records(records)
        self.assertEqual([r["name"] for r in ranked], ["杜甫", "苏轼", "陶渊明"])
        self.assertEqual([r["rank"] for r in ranked], [1, 1, 3])

    def test_rank_records_pure(self):
        records = [{"id": "a", "name": "杜甫", "score": 100}]
        ranked = PT.rank_records(records)
        self.assertNotIn("rank", records[0])          # 不修改入参
        self.assertEqual(ranked[0]["rank"], 1)

    def test_empty_ranking(self):
        self.assertEqual(PT.rank_records([]), [])


class TestOverrides(unittest.TestCase):
    """覆盖层：apply_overrides / apply_overrides_rich。"""

    def test_apply_overrides_pure(self):
        scores = {"u-屈原": 50, "u-其他": 60}
        n = PT.apply_overrides(scores, {"屈原": "u-屈原"})
        self.assertEqual(n, 1)
        self.assertEqual(scores["u-屈原"], 100)
        self.assertEqual(scores["u-其他"], 60)

    def test_apply_overrides_with_rows_subset(self):
        # 桩里只有 3 位有作者行：只覆盖这 3 位，其余跳过
        rows = {"屈原", "陶渊明", "王勃"}
        con = _StubCon(with_rows=rows)
        uid_map = PT.name_uid_map(con)
        self.assertEqual(set(uid_map), rows)
        scores = {u: 1 for u in uid_map.values()}
        self.assertEqual(PT.apply_overrides(scores, uid_map), 3)
        for name, uid in uid_map.items():
            self.assertEqual(scores[uid], PT.get_score(name))

    def test_apply_overrides_skips_rowless(self):
        # 12 位无作者行：name_uid 里没有 -> 跳过，不建键
        scores = {}
        n = PT.apply_overrides(scores, {})
        self.assertEqual(n, 0)
        self.assertEqual(scores, {})

    def test_apply_overrides_rich(self):
        con = _StubCon({"u-屈原": "chu", "u-李白": "tang"})
        orig = PT.name_uid_map
        PT.name_uid_map = lambda c: {"屈原": "u-屈原", "李白": "u-李白"}
        try:
            rich = [{"id": "u-李白", "name": "李白", "dynasty": "tang",
                     "score": 74, "n": 1211}]
            rich, ovr, add = PT.apply_overrides_rich(rich, con)
            # 李白：74 -> 100（覆盖）；屈原：无热度分诗词，补 n=0 记录进榜
            self.assertEqual((ovr, add), (1, 1))
            self.assertEqual(rich[0]["score"], 100)
            added = [r for r in rich if r["name"] == "屈原"]
            self.assertEqual(len(added), 1)
            self.assertEqual((added[0]["id"], added[0]["score"], added[0]["n"]),
                             ("u-屈原", 100, 0))
        finally:
            PT.name_uid_map = orig


class TestPureHeat(unittest.TestCase):
    """纯热度聚合与名次（留痕数据来源）。"""

    def test_pure_heat_and_ranks(self):
        rows = [("u1", 80), ("u1", 50), ("u2", 70), ("u3", 70)]
        names = {"u1": "甲", "u2": "乙", "u3": "丙"}

        class _Con:
            def execute(self, sql, params=()):
                s = " ".join(sql.split()).lower()
                if "author_uid, score" in s:
                    return _Cur(rows)
                if "uid, name from authors" in s:
                    return _Cur(names.items())
                raise AssertionError("unexpected SQL: %s" % sql)

        pure = PT.pure_heat(_Con())
        # 收缩估计（k=20）：μ=(80+50+70+70)/4=67.5；
        # u1=(130+20*67.5)/22=67.3->67；u2=u3=(70+20*67.5)/21=67.6->68
        self.assertEqual(pure["u1"], {"name": "甲", "score": 67, "n": 2})
        self.assertEqual(pure["u2"]["score"], 68)
        self.assertEqual(pure["u3"]["score"], 68)
        ranks = PT.heat_ranks(pure)
        self.assertEqual(ranks["u2"], 1)                # 68 分并列榜首
        self.assertEqual(ranks["u3"], 1)
        self.assertEqual(ranks["u1"], 3)                # 67 分第 3

    def test_build_records(self):
        pure = {"u-李白": {"name": "李白", "score": 74, "n": 1211}}
        uid_map = {"李白": "u-李白", "屈原": "u-屈原"}
        records = PT.build_records(uid_map, pure)
        by_name = {r["name"]: r for r in records}
        li = by_name["李白"]
        self.assertEqual((li["id"], li["tier"], li["score"]), ("u-李白", 1, 100))
        self.assertEqual((li["heat_score"], li["heat_rank"]), (74, 1))
        qu = by_name["屈原"]
        self.assertIsNone(qu["heat_score"])             # 无热度分 -> null 留痕
        self.assertIsNone(qu["heat_rank"])


class TestSync(unittest.TestCase):
    """DB 同步的安全闸与写入。"""

    def _records(self):
        pure = {"u-a": {"name": "晏几道", "score": 77, "n": 100},
                "u-b": {"name": "柳永", "score": 73, "n": 200},
                "u-li": {"name": "李白", "score": 74, "n": 1211}}
        records = [
            {"id": "u-qu", "name": "屈原", "tier": 1, "score": 100,
             "heat_score": None, "heat_rank": None},
            {"id": "u-li", "name": "李白", "tier": 1, "score": 100,
             "heat_score": 74, "heat_rank": 2},
        ]
        return records, pure, None

    def test_sync_db_writes_pure_plus_overrides(self):
        records, pure, _ = self._records()
        con = _SyncStubCon({"u-a": 77, "u-b": 73, "u-qu": 100, "u-li": 74})
        n = PT.sync_db(con, records, pure)
        self.assertEqual(n, 4)                          # 4 行非零分
        self.assertEqual(con.authors, {"u-a": 77, "u-b": 73,
                                       "u-qu": 100, "u-li": 100})
        self.assertEqual(con.meta["poet_tier_rubric_version"], "2.0")
        self.assertEqual(con.meta["poet_tier_count"], "2")

    def test_sync_db_resets_stale_tier_scores(self):
        # 旧标准残留（如第二档 88、第五档提升组 98）必须被重置
        records, pure, _ = self._records()
        con = _SyncStubCon({"u-a": 77, "u-b": 73, "u-qu": 100, "u-li": 100,
                            "u-stale": 88})
        # u-stale 不在 pure 也不在分级 -> 应被重置为 0
        PT.sync_db(con, records, pure)
        self.assertEqual(con.authors["u-stale"], 0)

    def test_guard_blocks_drift(self):
        records, pure, _ = self._records()
        con = _SyncStubCon({"u-a": 999, "u-b": 73, "u-qu": 100, "u-li": 74})
        with self.assertRaises(PT.TierError) as ctx:
            PT.sync_db(con, records, pure)
        self.assertIn("不一致", str(ctx.exception))
        self.assertEqual(con.authors["u-a"], 999)       # 未动数据

    def test_sync_heat_json_rebuilds_from_pure(self):
        records, pure, _ = self._records()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "author-heat-scores.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"meta": {"name": "author-heat-scores"},
                           "authors": [{"id": "u-a", "name": "晏几道",
                                        "score": 88, "rank": 1}]}, f)
            ovr, add, total = PT.sync_heat_json(path, records, pure)
            self.assertEqual((ovr, add, total), (1, 1, 4))   # 李白覆盖 + 屈原新增
            with open(path, encoding="utf-8") as f:
                back = json.load(f)
            by_id = {r["id"]: r for r in back["authors"]}
            self.assertEqual(by_id["u-li"]["score"], 100)    # 档位分覆盖纯热度
            self.assertEqual(by_id["u-qu"]["score"], 100)    # 无热度分诗人新增
            self.assertEqual(by_id["u-a"]["score"], 77)      # 纯热度分（旧 88 被重建掉）
            self.assertIn("tier_override", back["meta"])
            self.assertEqual(back["meta"]["n_records"], 4)
            # 名次重算：两个 100 并列第 1，77 第 3，73 第 4
            self.assertEqual(by_id["u-qu"]["rank"], 1)
            self.assertEqual(by_id["u-li"]["rank"], 1)
            self.assertEqual(by_id["u-a"]["rank"], 3)
            self.assertEqual(by_id["u-b"]["rank"], 4)


if __name__ == "__main__":
    unittest.main()
