# -*- coding: utf-8 -*-
"""Tests for ``loader/folding.py``.

The folding layer is the one part of the unified database with real
uncertainty — everything downstream assumes that folding preserves character
count and that a traditional and a simplified query produce the same index
lookup. These tests pin both down without needing the corpus.

The highest-value test here is ``test_update_ci_table_is_reproduced``: rather
than inventing cases, it replays the 17-entry variant table the corpus
maintainers hand-maintained in ``宋词/UpdateCi.py``. That table is a human
judgement about what needs folding in this corpus, so agreeing with it
validates the choice of zhconv for free.
"""
import json
import os
import sqlite3

import pytest

from loader import folding

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

#: ``宋词/UpdateCi.py``, verbatim. 16 of the 17 are traditional/simplified
#: pairs. The odd one out is 舃->舄, which is a glyph variant rather than a
#: T/S pair and therefore has to be carried by ``folding._VARIANT_PAIRS``.
UPDATE_CI_TABLE = {
    "鵷": "鹓",
    "颭": "飐",
    "鷁": "鹢",
    "鴞": "鸮",
    "餖": "饾",
    "飣": "饤",
    "舃": "舄",
    "駸": "骎",
    "薄倖": "薄幸",
    "赬": "赪",
    "鷫鸘": "鹔鹴",
    "嶮": "崄",
    "後": "后",
    "纇": "颣",
    "颸": "飔",
    "崑崙": "昆仑",
    "曨": "昽",
}

#: Traditional/simplified pairs whose two halves are identical in both
#: scripts, kept as a control: these must NOT change.
SAME_IN_BOTH_SCRIPTS = ["明月", "春风", "江山", "白云", "故人"]

#: Files small enough to read in a unit test, one per script and per body
#: key, so the length invariant is checked against real text.
SAMPLE_FILES = [
    "四书五经/daxue.json",       # traditional, bare dict root
    "四书五经/zhongyong.json",   # traditional, bare dict root
    "论语/lunyu.json",           # simplified, 'paragraphs'
    "诗经/shijing.json",         # simplified, 'content'
]


def _all_strings(node):
    """Every string anywhere in a parsed JSON document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _all_strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _all_strings(value)


@pytest.fixture(scope="module")
def sample_text():
    """Every string from the sample corpus files, as one list."""
    out = []
    for relative in SAMPLE_FILES:
        path = os.path.join(REPO_ROOT, relative)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            out.extend(_all_strings(json.load(handle)))
    assert out, "no sample corpus files were readable"
    return out


# --------------------------------------------------------------------------
# The maintainers' own table
# --------------------------------------------------------------------------

@pytest.mark.parametrize("source,expected", sorted(UPDATE_CI_TABLE.items()))
def test_update_ci_table_is_reproduced(source, expected):
    assert folding.fold(source) == expected


def test_update_ci_table_has_seventeen_entries():
    """Guards against the table here silently drifting from UpdateCi.py."""
    assert len(UPDATE_CI_TABLE) == 17


@pytest.mark.parametrize("source", sorted(UPDATE_CI_TABLE))
def test_update_ci_entries_preserve_length(source):
    folded, preserved = folding.fold_checked(source)
    assert preserved
    assert len(folded) == len(source)


# --------------------------------------------------------------------------
# The length invariant everything downstream depends on
# --------------------------------------------------------------------------

def test_fold_preserves_length_on_real_corpus_text(sample_text):
    """``len(fold(s)) == len(s)`` for every string in the sample files.

    This is the invariant that lets an index offset be used as an offset into
    the displayed original. Measured across all 16,403 distinct characters in
    the repository it holds; this test keeps it that way for a sample cheap
    enough to run in CI.
    """
    for text in sample_text:
        folded, preserved = folding.fold_checked(text)
        assert preserved, "folding changed the length of %r" % (text,)
        assert len(folded) == len(text)


def test_fold_does_not_break_offsets(sample_text):
    """A position found in folded text addresses the same character."""
    checked = 0
    for text in sample_text:
        for needle in ("明月", "春風", "春风", "不", "人"):
            at = text.find(needle)
            if at < 0:
                continue
            folded_at = folding.fold(text).find(folding.fold(needle))
            assert folded_at == at, (
                "offset drifted for %r in %r: %d != %d"
                % (needle, text, folded_at, at)
            )
            checked += 1
    assert checked > 0, "no sample text contained any of the probe needles"


def test_fold_is_idempotent(sample_text):
    for text in sample_text:
        once = folding.fold(text)
        assert folding.fold(once) == once


def test_fold_leaves_ascii_and_punctuation_alone():
    for text in ["abc 123", "，。！？", ""]:
        assert folding.fold(text) == text


def test_fold_only_touches_characters_that_need_it():
    """Punctuation around a traditional character is left in place."""
    assert folding.fold("《靜夜思》") == "《静夜思》"


# --------------------------------------------------------------------------
# Many-to-one folding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("後宮", "后宫"),
    ("皇后", "皇后"),
    ("幹活", "干活"),
    ("乾燥", "干燥"),
    ("一隻", "一只"),
    ("只是", "只是"),
    ("頭髮", "头发"),
])
def test_many_to_one_folding(text, expected):
    assert folding.fold(text) == expected


def test_many_to_one_folding_makes_a_single_character_query_a_superset():
    """後 and 后 collide, so a one-character query over-matches.

    That is a superset, never a wrong answer, and ``exact=True`` removes it —
    which is why no reverse index of ambiguous folds is kept.
    """
    assert "后" in folding.fold("後宮")
    assert "后" in folding.fold("皇后")
    assert folding.fold("後宮") != folding.fold("皇后")


def test_phrase_table_beats_character_folding():
    """乾隆 must not become 干隆, even though 乾 alone folds to 干."""
    assert folding.fold("乾") == "干"
    assert folding.fold("乾隆") == "乾隆"


# --------------------------------------------------------------------------
# Variant table
# --------------------------------------------------------------------------

def test_variant_pairs_replace_one_character_with_one():
    """Every pair must be length-preserving, or the offset mapping breaks."""
    for source, target in folding._VARIANT_PAIRS.items():
        assert len(source) == 1, source
        assert len(target) == 1, target
        assert folding.fold(source) == target


def test_glyph_variant_is_folded_even_though_zhconv_ignores_it():
    """舃/舄 is a glyph variant, not a T/S pair, so zhconv cannot do it.

    Both forms are in the corpus (舃 62 times, 舄 552 times) and zhconv returns
    each unchanged in every direction, so without the explicit table a search
    for either form would miss the other.
    """
    import zhconv

    assert zhconv.convert("舃", "zh-cn") == "舃", "zhconv started handling this"
    assert folding.fold("舃") == folding.fold("舄") == "舄"


# --------------------------------------------------------------------------
# segment()
# --------------------------------------------------------------------------

@pytest.mark.parametrize("source,expected", [
    ("春風萬里abc", "春 風 萬 里 abc"),
    ("明，月", "明， 月"),
    ("ABC123", "ABC123"),
    ("a b", "a b"),
    ("詩abc", "詩 abc"),
    ("白居易", "白 居 易"),
])
def test_segment_spacing(source, expected):
    assert folding.segment(source) == expected


@pytest.mark.parametrize("source", [
    "春風萬里abc", "明，月", "床前明月光，疑是地上霜。", "  多重   空格  ",
    "《靜夜思》", "", "abc", "，，，",
])
def test_segment_is_idempotent(source):
    once = folding.segment(source)
    assert folding.segment(once) == once


def test_segment_keeps_consecutive_ascii_in_one_token():
    """ASCII words stay searchable as words, not one token per letter."""
    assert folding.segment("ABCDEF").split() == ["ABCDEF"]


def test_segment_keeps_punctuation_so_exact_can_use_it():
    """Punctuation survives, so REPLACE(folded,' ','') recovers the text.

    That is what ``exact=True`` matches against: it makes 明，月 distinguishable
    from 明月, which is the whole point of the escape hatch.
    """
    segmented = folding.segment("明，月")
    assert segmented.replace(" ", "") == "明，月"


# --------------------------------------------------------------------------
# fts_query()
# --------------------------------------------------------------------------

@pytest.mark.parametrize("traditional,simplified", [
    ("春風", "春风"),
    ("長安", "长安"),
    ("萬里", "万里"),
    ("飛鳥", "飞鸟"),
    ("國家", "国家"),
    ("趙令畤", "赵令畤"),
    ("薄倖", "薄幸"),
    ("鷫鸘", "鹔鹴"),
])
def test_fts_query_traditional_and_simplified_agree(traditional, simplified):
    """The defect this whole layer exists to fix.

    Before folding, 春风 returned 0 rows against 全唐诗 where 春風 returned
    7251. Both spellings of a query must now compile to one index lookup.
    """
    assert folding.fts_query(traditional) == folding.fts_query(simplified)


@pytest.mark.parametrize("text", SAME_IN_BOTH_SCRIPTS)
def test_fts_query_is_a_quoted_phrase(text):
    query = folding.fts_query(text)
    assert query.startswith('"') and query.endswith('"')
    assert query == '"%s"' % " ".join(text)


@pytest.mark.parametrize("hostile", [
    '明月" OR "x',
    "明月*",
    "NEAR(a b)",
    "a AND b",
    "明月 -春風",
    "^春風",
    "春風)",
    "-明月",
])
def test_fts_query_neutralises_operators(hostile):
    """Everything is wrapped in one quoted phrase, so FTS5 sees literal text.

    Doubled double quotes are FTS5's escape inside a quoted string.
    """
    query = folding.fts_query(hostile)
    assert query.startswith('"') and query.endswith('"')
    # No bare quote may appear: every one must be part of a doubled pair.
    inner = query[1:-1]
    assert inner.replace('""', "").count('"') == 0


@pytest.mark.parametrize("empty", ["", "   ", "　", "，。！", "()", "-"])
def test_fts_query_rejects_queries_with_nothing_to_match(empty):
    """Punctuation-only input must raise, not silently match nothing."""
    with pytest.raises(ValueError):
        folding.fts_query(empty)


# --------------------------------------------------------------------------
# End to end against a real FTS5 table
# --------------------------------------------------------------------------

@pytest.fixture
def fts_table():
    """An in-memory FTS5 table built the way the real index is built."""
    connection = sqlite3.connect(":memory:")
    connection.execute(
        'CREATE VIRTUAL TABLE line_fts USING fts5('
        'folded, tokenize="unicode61 remove_diacritics 0")'
    )
    texts = [
        "春風萬里",       # traditional
        "春风万里",       # the same, simplified
        "明月",           # identical in both scripts
        "床前明月光，疑是地上霜。",
        "明，月",
    ]
    for rowid, text in enumerate(texts, start=1):
        connection.execute(
            "INSERT INTO line_fts(rowid, folded) VALUES (?, ?)",
            (rowid, folding.segment(folding.fold(text))),
        )
    yield connection
    connection.close()


def _match(connection, user_text):
    rows = connection.execute(
        "SELECT rowid FROM line_fts WHERE line_fts MATCH ? ORDER BY rowid",
        (folding.fts_query(user_text),),
    )
    return [row[0] for row in rows]


def test_fts_query_is_accepted_by_fts5(fts_table):
    """Every generated query must be valid MATCH input."""
    for text in ["春風", "明月", "明，月", "明月*", '明月" OR "x', "NEAR(a b)"]:
        _match(fts_table, text)  # must not raise


def test_fts5_finds_both_scripts(fts_table):
    """春風 and 春风 hit the same two rows: the traditional and the simplified one."""
    expected = [1, 2]
    assert _match(fts_table, "春風") == expected
    assert _match(fts_table, "春风") == expected


def test_fts5_punctuation_in_the_query_is_a_separator(fts_table):
    """明，月 and 明月 find the same rows — punctuation is editorial, not data."""
    assert _match(fts_table, "明，月") == _match(fts_table, "明月")


def test_fts5_matches_across_punctuation_in_the_corpus(fts_table):
    """明月 also reaches row 5, where the two characters are split by a comma.

    Rows 1 and 2 are 春風萬里/春风万里 and contain neither character, so 明月
    legitimately reaches rows 3, 4 and 5. That third row is the deliberate
    superset which ``exact=True`` exists to remove.
    """
    assert 5 in _match(fts_table, "明月")              # 明，月 matched
    assert _match(fts_table, "明月") == [3, 4, 5]


def test_fts5_hostile_queries_do_not_broaden_the_match(fts_table):
    """An injected OR must not turn into a real OR."""
    assert _match(fts_table, '明月" OR "春風') == []
