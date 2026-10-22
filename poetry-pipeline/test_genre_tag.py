# -*- coding: utf-8 -*-
"""genre_tag 的单元测试：体裁表完整性、判定规则、文本级标注的字节级安全性。

运行：python -m unittest test_genre_tag -v（在 poetry-pipeline 目录下）
"""

import json
import unittest

import genre_tag as G


def rec(title, paragraphs, author="佚名"):
    return {"author": author, "title": title, "paragraphs": paragraphs}


class TestGenreTable(unittest.TestCase):
    """体裁登记表：ID 唯一、连续、与名称/代码双向一致。"""

    def test_ids_are_unique_and_contiguous(self):
        ids = [g[0] for g in G.GENRES]
        self.assertEqual(ids, list(range(1, len(G.GENRES) + 1)),
                         "ID 必须是 1..N 的连续整数，删改体裁时不能留空洞")

    def test_names_and_codes_unique(self):
        names = [g[2] for g in G.GENRES]
        codes = [g[1] for g in G.GENRES]
        self.assertEqual(len(set(names)), len(names), "体裁中文名不允许重名")
        self.assertEqual(len(set(codes)), len(codes), "体裁 code 不允许重复")

    def test_bidirectional_lookup(self):
        for gid, code, name, _desc in G.GENRES:
            self.assertEqual(G.BY_ID[gid], (gid, code, name, _desc))
            self.assertEqual(G.BY_NAME[name], (gid, code, name, _desc))
            self.assertTrue(code.isascii() and code.islower())


class TestClassify(unittest.TestCase):
    """按体裁的典型样本。判定顺序即优先级，这里锁住的是“不许回退”的那几条。"""

    @classmethod
    def setUpClass(cls):
        G._tables()

    def test_wujue_is_shi(self):
        r = rec("登鹳雀楼", ["白日依山尽，黄河入海流。", "欲穷千里目，更上一层楼。"])
        self.assertEqual(G.classify("近现代", r), "诗")

    def test_ci_by_tune(self):
        r = rec("浣溪沙·一曲新词酒一杯", [
            "一曲新词酒一杯，去年天气旧亭台。夕阳西下几时回？",
            "无可奈何花落去，似曾相识燕归来。小园香径独徘徊。"])
        self.assertEqual(G.classify("清诗", r), "词")

    def test_qu_by_tune_in_qu_folder(self):
        r = rec("天净沙·秋思", ["枯藤老树昏鸦，小桥流水人家，古道西风瘦马。",
                              "夕阳西下，断肠人在天涯。"])
        self.assertEqual(G.classify("金", r), "曲")

    def test_fu_by_title(self):
        r = rec("秋声赋", [
            "欧阳子方夜读书，闻有声自西南来者，悚然而听之，曰：异哉！",
            "初淅沥以萧飒，忽奔腾而砰湃，如波涛夜惊，风雨骤至。",
            "其触于物也，鏦鏦铮铮，金铁皆鸣；又如赴敌之兵，衔枚疾走，不闻号令，但闻人马之行声。",
            "予谓童子：此何声也？汝出视之。童子曰：星月皎洁，明河在天，四无人声，声在树间。"])
        self.assertEqual(G.classify("清诗", r), "赋")

    def test_fu_de_is_not_fu(self):
        # “赋得”是动词用法：赋得古原草送别
        r = rec("赋得古原草送别", ["离离原上草，一岁一枯荣。", "野火烧不尽，春风吹又生。"])
        self.assertEqual(G.classify("清诗", r), "诗")

    def test_wen_by_title(self):
        r = rec("滕王阁序", ["豫章故郡，洪都新府。星分翼轸，地接衡庐。"])
        self.assertEqual(G.classify("近现代", r), "文")

    def test_sao_by_xixi(self):
        r = rec("九歌·湘夫人", ["帝子降兮北渚，目眇眇兮愁予。", "袅袅兮秋风，洞庭波兮木叶下。"])
        self.assertEqual(G.classify("魏晋", r), "骚")

    def test_lian(self):
        r = rec("太虚幻境对联", ["假作真时真亦假，无为有处有还无。"])
        self.assertEqual(G.classify("清诗", r), "联")

    def test_liuyan_shi_before_wen(self):
        # “同志”的“志”会命中文体尾字表，标题强标记必须优先
        r = rec("六言诗·给彭德怀同志", ["山高路远坑深，大军纵横驰奔。",
                                  "谁敢横刀立马？唯我彭大将军。"])
        self.assertEqual(G.classify("近现代", r), "诗")

    def test_xinshi(self):
        paras = ["母亲，好久以来就想为你写一首诗"] + ["但写了好多次还是没有写好"] * 11
        r = rec("写给母亲的诗", paras)
        self.assertEqual(G.classify("近现代", r), "新诗")

    def test_tangqian_lefu_not_ci(self):
        # 唐前没有严格意义的词，《子夜歌》是乐府，不能被词牌表带走
        r = rec("子夜歌", ["落日出前门，瞻瞩见子度。", "冶容多姿鬓，芳香已盈路。"])
        self.assertEqual(G.classify("南北朝", r), "诗")


class TestAnnotateText(unittest.TestCase):
    """文本级外科编辑：只增一行 genreId，缩进/换行照抄原文件。"""

    def test_adds_genre_id_and_keeps_everything_else(self):
        text = '[\r\n {\r\n  "author": "甲",\r\n  "title": "乙",\r\n  "id": "x"\r\n }\r\n]'
        rows = [{"author": "甲", "title": "乙", "id": "x"}]
        new = G.annotate_text(text, rows, [1])
        self.assertEqual(json.loads(new), [{"author": "甲", "title": "乙", "id": "x",
                                            "genreId": 1}])
        self.assertIn('\r\n  "genreId": 1\r\n }\r\n]', new)
        self.assertEqual(new.count("\n"), text.count("\n") + 1, "只许多一行")
        self.assertNotIn('"genre"', new, "名称不写进源数据（v3.5 只留 genreId）")

    def test_strips_legacy_name_field(self):
        # 老版本写进去的是两行（genreId + genre），重标之后只剩 id，且不留多余逗号
        text = ('[\r\n {\r\n  "title": "乙",\r\n  "genreId": 2,\r\n'
                '  "genre": "词"\r\n }\r\n]')
        new = G.annotate_text(text, [{"title": "乙", "genreId": 2, "genre": "词"}], [2])
        self.assertEqual(json.loads(new), [{"title": "乙", "genreId": 2}])
        self.assertEqual(new, '[\r\n {\r\n  "title": "乙",\r\n  "genreId": 2\r\n }\r\n]')

    def test_lf_files_stay_lf(self):
        text = '[\n    {\n        "title": "乙"\n    }\n]'
        new = G.annotate_text(text, [{"title": "乙"}], [6])
        self.assertNotIn("\r", new)
        self.assertIn('\n        "genreId": 6', new)

    def test_idempotent(self):
        text = '[\n {\n  "title": "乙"\n }\n]'
        once = G.annotate_text(text, [{"title": "乙"}], [2])
        twice = G.annotate_text(once, [{"title": "乙", "genreId": 2}], [2])
        self.assertEqual(once, twice, "重复标注不得叠加字段")

    def test_regrade_updates_value_in_place(self):
        # 改口径重标时只改数字，不动行数/位置
        text = '[\n {\n  "title": "乙",\n  "genreId": 2\n }\n]'
        new = G.annotate_text(text, [{"title": "乙", "genreId": 2}], [1])
        self.assertEqual(new, '[\n {\n  "title": "乙",\n  "genreId": 1\n }\n]')

    def test_record_count_mismatch_raises(self):
        text = '[\n {\n  "title": "乙"\n }\n]'
        with self.assertRaises(RuntimeError):
            G.annotate_text(text, [{"title": "乙"}, {"title": "丙"}], [1, 1])


class TestSchemaGenreTable(unittest.TestCase):
    """建库侧：schema.GENRES 是权威定义，kind 默认映射必须覆盖全部 kind。"""

    def test_kind_genre_covers_all_kinds(self):
        import schema as S
        for kind in S.KIND:
            self.assertIn(kind, S.KIND_GENRE, "kind=%s 没有默认体裁" % kind)
            self.assertIn(S.KIND_GENRE[kind], S.GENRE_NAME)

    def test_genre_ids_match_genre_tag(self):
        import schema as S
        self.assertEqual(G.GENRES, S.GENRES, "genre_tag 必须直接用 schema 的表")

    def test_source_table_is_gone(self):
        """v3.7：朝代表已删。朝代在 id 高位里，不需要 schema 再导出 SOURCE_ROWS。"""
        import schema as S
        self.assertFalse(hasattr(S, "SOURCE_ROWS"), "SOURCE_ROWS 应随 source 表一起删掉")
        self.assertFalse(hasattr(S, "SOURCE_NAME"))
        # 但解码表本身不能少：id 高位仍要靠它反解成中文朝代名
        self.assertEqual(len(S.DYNASTY), len(S.DYNASTY_LABEL))
        for key, code in S.DYNASTY.items():
            self.assertIn(key, S.DYNASTY_LABEL, "%s 没有中文名" % key)

    def test_poem_has_no_redundant_columns(self):
        """Poem 只派生 genre（展示用）；dynasty_id 属性随 dynasty_id 列一起删掉了。"""
        import schema as S
        pid = S.make_id("tang", "ci", 7)
        p = S.Poem(id=pid, dynasty="tang", kind="ci", title="t", author="", lines=["x"],
                   genre_id=2)
        self.assertFalse(hasattr(p, "dynasty_id"), "dynasty_id 列已删，属性也不该留")
        self.assertEqual(p.genre, "词")
        self.assertEqual(pid >> (S.KIND_BITS + S.SEQ_BITS), S.DYNASTY["tang"],
                         "朝代仍由 id 高位反解")
        self.assertEqual(S.Poem(id=pid, dynasty="tang", kind="ci", title="t", author="",
                                lines=["x"]).genre, "", "未标注时为空串而不是猜一个")


if __name__ == "__main__":
    unittest.main()
