# -*- coding: utf-8 -*-
"""Shared pytest setup for the repository.

Puts the repository root on ``sys.path`` so tests can ``import loader.*``
regardless of the directory pytest was invoked from, and registers the
``slow`` marker used by the corpus-wide tests.

The ``slow`` marker exists because most of this repository's data is far too
large to parse in CI: 2246 JSON files, 345 MB on disk, 70 MB of body text.
Tests marked ``slow`` read the real corpus and are expected to be deselected
with ``-m "not slow"``.
"""
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT


@pytest.fixture(scope="session")
def datas_config():
    """The parsed loader/datas.json, shared across the session."""
    with open(os.path.join(REPO_ROOT, "loader", "datas.json"), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# The synthetic corpus
# --------------------------------------------------------------------------
# Every behavioural test runs on this and not on the real corpus: 200+ MB is
# not something CI can parse, and a test that has to reach into 全唐诗/ to
# check what folding does is a test nobody will run. The point is to reproduce
# the corpus' *shapes* — traditional text, simplified text, a body under
# ``content`` as a string and as a list, an ``id`` that is null, a dataset
# whose records carry no author — at a size where the expected counts can be
# written down by hand and checked exactly.
#
# The numbers below are load-bearing: every test in test_build.py asserts
# against them, so changing a record here means changing those tests.

MINI_FILES = {
    # 全唐诗's shape: a list of records, body under ``paragraphs``, a UUID in
    # ``id``, sparse ``tags``, and one record whose id is null (which the real
    # 唐诗补录.json also contains, and which no reader may assume away).
    "trad/poet.tang.0.json": [
        {"id": "t-1", "title": "春風", "author": "李白",
         "paragraphs": ["春風不相識", "何事入羅幃"], "tags": ["五言絕句"]},
        # Three lines, two of which contain 明月, so that one work with two
        # matching lines is exercised: query.search has to return it once with
        # both lines, not twice. The third line is not the received text of
        # 静夜思 — the corpus prints 舉頭望山月 — it is here because no other
        # pair of lines in the fixture shares a character.
        {"id": "t-2", "title": "靜夜思", "author": "李白",
         "paragraphs": ["牀前明月光", "疑是地上霜", "舉頭望明月"]},
        # 明 ends one clause and 月 starts the next. The FTS phrase "明 月"
        # matches this (tokens are adjacent); the exact filter does not
        # (the literal substring 明月 is absent). Six real 全唐诗 lines have
        # exactly this shape — see test_exact_filter_excludes_clause_break.
        {"id": "t-3", "title": "夜坐", "author": "王建",
         "paragraphs": ["夜氣清而明，月照空山"]},
        {"id": None, "title": "無題", "author": "無名氏",
         "paragraphs": ["繁簡混雜測試"]},
    ],
    # Same dataset, second file, different dynasty — this is what
    # dynasty_rules is for.
    "trad/poet.song.0.json": [
        {"id": "s-1", "title": "相見歡", "author": "李煜",
         "paragraphs": ["無言獨上西樓", "月如鉤"]},
    ],
    # One poem, twice, under two titles, in two files of one dataset. This is
    # 全唐诗's real shape: 出塞 is "横吹曲辞 出塞 一" in poet.tang.0.json and
    # "出塞二首 一" in poet.tang.6000.json, byte for byte the same body, and
    # the same again in 唐诗三百首.json. 6,503 groups of works in the built
    # database are shaped like this, and query.search has to return one result
    # for them rather than one per anthology.
    "trad/poet.tang.1.json": [
        {"id": "t-4", "title": "橫吹曲辭 出塞", "author": "王昌齡",
         "paragraphs": ["秦時明月漢時關", "萬里長征人未還"]},
        {"id": "t-5", "title": "出塞二首 其一", "author": "王昌齡",
         "paragraphs": ["秦時明月漢時關", "萬里長征人未還"]},
    ],
    # 幽梦影's shape: no title, no author, and ``content`` is a single string
    # rather than a list. The whole string is one line.
    "qing/youmengying.json": [
        {"content": "春風解凍，和氣消冰"},
    ],
    # 楚辞's shape: the same key name as the file above, but a list.
    "gu/chuci.json": [
        {"title": "離騷", "author": "屈原",
         "content": ["帝高陽之苗裔兮", "朕皇考曰伯庸"]},
        # A space between two Han characters. The real corpus has only four
        # lines containing a space at all and none of them is shaped like
        # this, which is exactly why it has to be synthesised: it is the one
        # input that tells a literal substring test against line.text apart
        # from a de-spaced test against line.folded. See
        # test_exact_filter_does_not_invent_a_match_across_a_space.
        {"title": "卜居", "author": "屈原",
         "content": ["尺有所短 寸有所长"]},
    ],
}

MINI_DATASETS = {
    "tang": {
        "name": "全唐诗", "id": 0, "path": "trad/", "tag": "paragraphs",
        "kind": "shi",
        "dynasty_rules": [{"match": "poet.song.*", "dynasty": "song"},
                          {"match": "*", "dynasty": "tang"}],
        "mapping": {"root": "list", "title": "$.title", "author": "$.author",
                    "body": "$.paragraphs", "external_id": "$.id",
                    "tags": "$.tags"},
    },
    "qing": {
        "name": "幽梦影", "id": 1, "path": "qing/youmengying.json",
        "tag": "content", "dynasty": "qing", "kind": "wen",
        "default_author": "张潮",
        "mapping": {"root": "list", "title": "$.title", "author": "$.author",
                    "body": "$.content"},
    },
    "xianqin": {
        "name": "楚辞", "id": 2, "path": "gu/", "tag": "content",
        "dynasty": "xianqin", "kind": "fu",
        "mapping": {"root": "list", "title": "$.title", "author": "$.author",
                    "body": "$.content"},
    },
}

#: Totals over MINI_FILES. Asserted verbatim in test_build.py.
MINI_WORKS = 10
MINI_LINES = 17
MINI_AUTHORS = 7  # 李白 王建 无名氏 李煜 张潮 屈原 王昌龄


class MiniCorpus:
    """A built-in-miniature repository, plus the paths to build it."""

    def __init__(self, root):
        self.root = root
        self.config = os.path.join(root, "datas.json")
        self.db = os.path.join(root, "poetry.db")
        self.exceptions = os.path.join(root, "fold_exceptions.json")

    def write(self):
        for relative, records in MINI_FILES.items():
            path = os.path.join(self.root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(records, handle, ensure_ascii=False)
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump({"datasets": MINI_DATASETS}, handle, ensure_ascii=False)
        return self

    def build(self, **kwargs):
        from loader import build_db

        kwargs.setdefault("full", True)
        return build_db.build(self.db, repo_root=self.root,
                              config_path=self.config,
                              exceptions_path=self.exceptions, **kwargs)

    def connect(self):
        from loader import build_db

        return build_db.connect(self.db)

    def rows(self, sql, *args):
        conn = self.connect()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def one(self, sql, *args):
        return self.rows(sql, *args)[0][0]


@pytest.fixture
def mini_corpus(tmp_path):
    """A synthetic corpus written to a temporary directory.

    Deliberately not built: a test that only wants to exercise the path
    grammar should not pay for a database, and test_build.py is explicit
    about when it builds.
    """
    return MiniCorpus(str(tmp_path)).write()


@pytest.fixture
def mini_db(mini_corpus):
    """The same corpus, built. Yields the corpus object with ``.db`` populated."""
    mini_corpus.build()
    return mini_corpus

