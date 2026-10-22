"""sources 的单元测试：所有 Collection / AuthorSource 的字典字段必须闭合在 schema 里。

运行：python -m unittest test_sources -v

为什么需要这组测试：`Collection.dynasty` 与 `AuthorSource.dynasty` 是**裸字符串**，
拼错了不会有任何编译期提示，只会安静地把非法值写进库里。真实事故：AuthorSource
的第二参数曾填成集合名（chuci / nalan / mengxue），15 位作者的 authors.dynasty
因此落在 schema.DYNASTY 之外，而 verify_db.py 当时也没有断言盯着这一列，
于是 3.5→3.7 三次重建全部 PASS，脏数据一路带了下来。
"""

import unittest

import schema as S
import sources as SRC


class TestCollectionDicts(unittest.TestCase):
    def test_every_collection_dynasty_is_known(self):
        bad = [(c.name, c.dynasty) for c in SRC.COLLECTIONS if c.dynasty not in S.DYNASTY]
        self.assertEqual(bad, [], "未知 dynasty：%r" % (bad,))

    def test_every_collection_kind_is_known(self):
        bad = [(c.name, c.kind) for c in SRC.COLLECTIONS if c.kind not in S.KIND]
        self.assertEqual(bad, [], "未知 kind：%r" % (bad,))

    def test_collection_genre_override_is_known(self):
        """genre=0 是「不指定」（由 KIND_GENRE 按 kind 推），不是非法值。"""
        known = {g[0] for g in S.GENRES} | {0}
        bad = [(c.name, c.genre) for c in SRC.COLLECTIONS if c.genre not in known]
        self.assertEqual(bad, [], "未知 genre id：%r" % (bad,))

    def test_collection_names_are_unique(self):
        names = [c.name for c in SRC.COLLECTIONS]
        self.assertEqual(len(names), len(set(names)), "集合名重复")


class TestAuthorSourceDicts(unittest.TestCase):
    def test_every_author_source_dynasty_is_known(self):
        """AuthorSource 第二个参数是 dynasty，不是来源名——填错就写进 authors.dynasty。"""
        bad = [(a.name, a.dynasty) for a in SRC.AUTHOR_SOURCES if a.dynasty not in S.DYNASTY]
        self.assertEqual(bad, [], "authors.dynasty 会落到 DYNASTY 编码之外：%r" % (bad,))

    def test_author_source_named_after_a_collection_shares_its_dynasty(self):
        """`xxx.authors` 与名为 `xxx` 的集合若同源，两者 dynasty 必须一致。

        这是上面那起事故的正面表述：楚辞集合是 preqin，作者源就不能是 chuci。
        """
        colls = {c.name: c for c in SRC.COLLECTIONS}
        mismatched = []
        for a in SRC.AUTHOR_SOURCES:
            coll = colls.get(a.name.split(".", 1)[0])
            if coll is not None and coll.dynasty != a.dynasty:
                mismatched.append((a.name, a.dynasty, coll.dynasty))
        self.assertEqual(mismatched, [],
                         "作者源与同名集合的朝代不一致（name, 作者源, 集合）：%r" % (mismatched,))

    def test_known_corpus_dynasties(self):
        """三处曾经写错的值，钉死成回归断言。"""
        by_name = {a.name: a.dynasty for a in SRC.AUTHOR_SOURCES}
        self.assertEqual(by_name["chuci.authors"], "preqin")
        self.assertEqual(by_name["nalan.authors"], "qing")
        self.assertEqual(by_name["mengxue.authors"], "unknown")

    def test_author_source_names_are_unique(self):
        names = [a.name for a in SRC.AUTHOR_SOURCES]
        self.assertEqual(len(names), len(set(names)), "作者源名重复")


class TestSchemaDictCoverage(unittest.TestCase):
    def test_dynasty_label_covers_every_code(self):
        missing = [k for k in S.DYNASTY if k not in S.DYNASTY_LABEL]
        self.assertEqual(missing, [], "DYNASTY_LABEL 缺中文名：%r" % (missing,))

    def test_dynasty_codes_fit_in_dyn_bits(self):
        """编码必须塞得进 DYN_BITS 位，否则 id 高位会溢出到 kind 段。"""
        overflow = [(k, v) for k, v in S.DYNASTY.items() if not 0 <= v < (1 << S.DYN_BITS)]
        self.assertEqual(overflow, [], "编码超出 DYN_BITS=%d：" % S.DYN_BITS, )

    def test_genre_rows_are_unique_and_dense(self):
        ids = [g[0] for g in S.GENRES]
        self.assertEqual(ids, list(range(1, len(ids) + 1)), "genre id 必须从 1 连续编号")
        names = [g[2] for g in S.GENRES]
        self.assertEqual(len(names), len(set(names)), "genre 名称重复")


if __name__ == "__main__":
    unittest.main()
