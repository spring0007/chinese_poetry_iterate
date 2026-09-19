# -*- coding: utf-8 -*-
"""The builder, against the synthetic corpus in conftest.py.

Everything here runs on ``mini_db``: ten works and seventeen lines whose
expected contents are written down in conftest.py, not on the 200 MB real
corpus. The shapes under test — traditional source text, a body under
``content`` as both a string and a list, an ``id`` that is null, a dataset
whose records carry no author, one poem filed twice under two titles — are the
shapes the real corpus has; the volume is not needed to check any of them, and
a slow test is a test that gets skipped.
"""
from conftest import MINI_AUTHORS, MINI_FILES, MINI_LINES, MINI_WORKS

import pytest

from loader import folding, query

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def line_ids(corpus, text):
    """The line ids an FTS query matches, by the same phrase rule the CLI uses."""
    return {row[0] for row in corpus.rows(
        "SELECT line.id FROM line_fts JOIN line ON line.id = line_fts.rowid "
        "WHERE line_fts MATCH ?", folding.fts_query(text))}


def exact_line_ids(corpus, text):
    """``line_ids`` narrowed by the literal-substring filter ``exact=True`` applies.

    Delegates to ``loader.query``, which owns the expression now that there is
    a caller. ``test_query.py`` pins the contract independently — against a
    Python substring test over the loose hit set — so this helper agreeing with
    ``query.matching_line_ids`` is a fact about one implementation rather than
    proof that it is right.
    """
    conn = corpus.connect()
    try:
        return query.matching_line_ids(conn, text, exact=True)
    finally:
        conn.close()


def texts_of(corpus, ids):
    """The display text of a set of line ids, as a set.

    Tests that used to pop the single expected row say what they mean as a set
    instead: the fixture carries one poem twice, so "the match" is no longer a
    well-defined row.
    """
    return {corpus.one("SELECT text FROM line WHERE id = ?", line_id)
            for line_id in sorted(ids)}


def traditional_chars_in_sources():
    """Every character the fixtures spell in traditional form.

    Derived from the fixture data rather than hard-coded, so adding a record
    to conftest.py extends the leak scan automatically instead of leaving a
    stale list behind that quietly stops covering the new text.
    """
    chars = set()
    for records in MINI_FILES.values():
        for record in records:
            for value in record.values():
                for text in (value if isinstance(value, list) else [value]):
                    if isinstance(text, str):
                        chars.update(ch for ch in text if folding.fold(ch) != ch)
    return chars


# --------------------------------------------------------------------------
# what got written
# --------------------------------------------------------------------------

def test_counts(mini_db):
    assert mini_db.one("SELECT count(*) FROM work") == MINI_WORKS
    assert mini_db.one("SELECT count(*) FROM line") == MINI_LINES
    assert mini_db.one("SELECT count(*) FROM author") == MINI_AUTHORS
    assert mini_db.one("SELECT count(*) FROM source_file") == len(MINI_FILES)
    assert mini_db.one("SELECT count(*) FROM dataset") == 3


def test_line_count_column_agrees_with_the_rows(mini_db):
    """``work.line_count`` is denormalised; it must not drift from reality."""
    assert mini_db.one("SELECT sum(line_count) FROM work") == MINI_LINES
    assert mini_db.one(
        "SELECT count(*) FROM work WHERE line_count <> "
        "(SELECT count(*) FROM line WHERE line.work_id = work.id)") == 0


def test_no_orphans(mini_db):
    assert mini_db.one(
        "SELECT count(*) FROM line WHERE work_id NOT IN (SELECT id FROM work)") == 0
    assert mini_db.one("SELECT count(*) FROM work WHERE author_id IS NULL") == 0


def test_build_run_is_recorded(mini_db):
    row = mini_db.rows(
        "SELECT mode, finished_at, files_parsed, works_written, lines_written "
        "FROM build_run ORDER BY id DESC LIMIT 1")[0]
    assert row[0] == "full"
    assert row[1] is not None          # finished_at set only on success
    assert row[2] == len(MINI_FILES)
    assert row[3] == MINI_WORKS
    assert row[4] == MINI_LINES


def test_schema_meta_carries_both_versions(mini_db):
    meta = dict(mini_db.rows("SELECT key, value FROM schema_meta"))
    assert meta["folding_version"] == folding.__version__
    assert meta["schema_version"]
    assert meta["generator"].startswith("loader.build_db/")


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------

def test_fts_mirrors_its_content_table(mini_db):
    """The trigger trios keep the three indexes in step with their tables.

    A disagreement here is the silent-corruption case schema.sql describes:
    an external-content FTS5 table holds no copy of its own text, so a missed
    'delete' leaves a row that no query can see and no check reports.
    """
    for table in ("line", "work", "author"):
        assert mini_db.one("SELECT count(*) FROM %s_fts" % table) == \
            mini_db.one("SELECT count(*) FROM %s" % table), table


@pytest.mark.parametrize("table", ["line_fts", "work_fts", "author_fts"])
def test_integrity_check(mini_db, table):
    mini_db.rows("INSERT INTO %s(%s) VALUES ('integrity-check')" % (table, table))


def test_deleting_a_work_takes_its_lines_out_of_the_index(mini_db):
    """The cascade has to fire the line delete trigger, not just the row delete.

    This is the corruption schema.sql warns about: an external-content FTS5
    table re-reads the content table to delete a row, so a cascade that
    removed the lines without firing ``line_ad`` would leave the index
    holding rows for text that no longer exists — and ``integrity-check``
    would still pass. The fixture has two 春风 lines, one of which is about to
    be deleted, so a missed trigger shows up as 2 instead of 1.
    """
    assert len(line_ids(mini_db, "春風")) == 2
    conn = mini_db.connect()
    try:
        lines = conn.execute(
            "SELECT count(*) FROM line WHERE work_id = "
            "(SELECT id FROM work WHERE external_id = 't-1')").fetchone()[0]
        assert lines == 2
        conn.execute("DELETE FROM work WHERE external_id = 't-1'")
        conn.commit()
    finally:
        conn.close()

    assert mini_db.one("SELECT count(*) FROM line") == MINI_LINES - lines
    assert mini_db.one("SELECT count(*) FROM line_fts") == MINI_LINES - lines
    assert len(line_ids(mini_db, "春風")) == 1
    # and the index is still internally consistent afterwards
    mini_db.rows("INSERT INTO line_fts(line_fts) VALUES ('integrity-check')")


# --------------------------------------------------------------------------
# the bulk-index path a full build takes
# --------------------------------------------------------------------------

def test_a_full_build_leaves_every_trigger_in_place(mini_db):
    """The triggers come down for the build; they have to go back up.

    Nothing else checks this. A database that lost a trigger still answers
    every query correctly — until the day something deletes a row and the
    index quietly keeps it.
    """
    from loader import build_db

    present = {row[0] for row in mini_db.rows(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert present == set(build_db.schema_trigger_names())


def test_the_index_was_rebuilt_and_not_left_empty(mini_db):
    """A 'rebuild' that indexed nothing would still leave counts agreeing at zero."""
    assert mini_db.one("SELECT count(*) FROM line_fts") == MINI_LINES
    assert len(line_ids(mini_db, "春風")) == 2


def test_a_full_build_takes_the_triggers_down_while_it_writes(mini_corpus, monkeypatch):
    """Otherwise the two tests above pass for the wrong reason.

    They only compare the schema before and after a build, so a build that
    stopped taking the triggers down would satisfy both while doing none of
    the work this section is about. This watches the moment they are down.
    """
    from loader import build_db

    real = build_db.drop_fts_triggers
    observed = []

    def watched(conn):
        ddl = real(conn)
        observed.append(len(ddl))
        observed.append(conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0])
        return ddl

    monkeypatch.setattr(build_db, "drop_fts_triggers", watched)
    mini_corpus.build()

    expected = len(build_db.schema_trigger_names())
    assert expected > 1, "schema.sql should define a trigger trio per index"
    assert observed == [expected, 0]


def test_a_database_missing_a_trigger_is_refused(mini_corpus):
    """Refused loudly, because the alternative is a silently stale index.

    Only a killed process gets a database into this state, so the test has to
    put it there by hand. Without the guard the next partial build would run
    to completion and report success.
    """
    mini_corpus.build()
    mini_corpus.rows("DROP TRIGGER line_ai")
    with pytest.raises(SystemExit) as caught:
        mini_corpus.build(full=False, only=["qing"])
    assert "line_ai" in str(caught.value)


def test_a_build_that_fails_restores_the_triggers(mini_corpus, monkeypatch):
    """The repair runs on the failure path too, and does not mask the error."""
    from loader import build_db

    def explode(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(build_db, "build_file", explode)
    with pytest.raises(RuntimeError):
        mini_corpus.build()

    present = {row[0] for row in mini_corpus.rows(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert present == set(build_db.schema_trigger_names())


# --------------------------------------------------------------------------
# folding, which is what the user asked for
# --------------------------------------------------------------------------

def test_traditional_and_simplified_find_the_same_lines(mini_db):
    """The whole point of folding: one query, one result set, either script."""
    for traditional, simplified in (("春風", "春风"), ("明月", "明月"),
                                    ("無言獨上西樓", "无言独上西楼")):
        assert line_ids(mini_db, traditional) == line_ids(mini_db, simplified)
        assert line_ids(mini_db, simplified), "fixture should match something"


def test_chunfeng_matches_across_datasets(mini_db):
    """一枝春風 in 全唐诗 and 春風解凍 in 幽梦影 are two datasets, one index."""
    hits = line_ids(mini_db, "春风")
    assert len(hits) == 2
    works = {row[0] for row in mini_db.rows(
        "SELECT DISTINCT work.dataset_id FROM line JOIN work ON work.id = line.work_id "
        "WHERE line.id IN (%s)" % ",".join("?" * len(hits)), *sorted(hits))}
    assert len(works) == 2, "the two hits should come from two different datasets"


def test_no_traditional_character_survives_anywhere(mini_db):
    """The user's instruction: store Simplified only, drop the traditional.

    Every character the fixtures spell traditionally is checked against every
    text column. If the builder ever stops folding one of them, this fails
    with the character named rather than as an off-by-N count somewhere else.
    """
    chars = traditional_chars_in_sources()
    assert chars, "fixture derivation broke — this test would be vacuous"
    for ch in sorted(chars):
        for column, table in (("text", "line"), ("title", "work"),
                              ("rhythmic", "work"), ("author_name", "work"),
                              ("name", "author")):
            hits = mini_db.one(
                "SELECT count(*) FROM %s WHERE %s LIKE ?" % (table, column),
                "%" + ch + "%")
            assert hits == 0, "%s.%s still contains %r" % (table, column, ch)


def test_text_is_the_expected_simplified_string(mini_db):
    assert mini_db.one(
        "SELECT text FROM line WHERE work_id = "
        "(SELECT id FROM work WHERE external_id = 't-1') AND idx = 0") == "春风不相识"
    assert mini_db.one(
        "SELECT title FROM work WHERE external_id = 't-1'") == "春风"
    assert mini_db.one(
        "SELECT title_folded FROM work WHERE external_id = 't-1'") == "春 风"


def test_folded_column_equals_segment_of_text(mini_db):
    """The shadow column must be segment(text) — the index depends on it."""
    for text, folded in mini_db.rows("SELECT text, folded FROM line"):
        assert folded == folding.segment(text)


# --------------------------------------------------------------------------
# the exact=True escape hatch
# --------------------------------------------------------------------------

def test_phrase_search_tolerates_a_clause_break(mini_db):
    """明，月 matches "明 月" as a phrase: punctuation is not a token.

    Five rows: 床前明月光 and 舉頭望明月 (two lines of one 静夜思), 秦时明月汉
    时关 twice over (the poem the fixture files under two titles — distinct rows
    in distinct works), and 夜气清而明，月照空山, where a comma is the only thing
    between the two characters.
    """
    assert len(line_ids(mini_db, "明月")) == 5


def test_exact_filter_excludes_clause_break(mini_db):
    """...and the literal filter drops it, leaving only the contiguous 明月."""
    loose = line_ids(mini_db, "明月")
    exact = exact_line_ids(mini_db, "明月")
    assert len(exact) == 4
    assert exact < loose
    assert texts_of(mini_db, loose - exact) == {"夜气清而明，月照空山"}


def test_exact_filter_matches_the_literal_it_was_given(mini_db):
    """The same row survives or not depending on the literal, not the tokens.

    ``明，月`` and ``明月`` tokenise to the same phrase and so find the same
    rows — but only one of them is the literal text of the 夜坐 line, and the
    filter must tell them apart. A filter that re-searched the phrase instead
    of testing a substring would return both rows for both queries.
    """
    assert line_ids(mini_db, "明，月") == line_ids(mini_db, "明月")
    assert texts_of(mini_db, exact_line_ids(mini_db, "明，月")) == {
        "夜气清而明，月照空山"}
    assert texts_of(mini_db, exact_line_ids(mini_db, "明月")) == {
        "床前明月光", "举头望明月", "秦时明月汉时关"}


def test_exact_filter_does_not_invent_a_match_across_a_space(mini_db):
    """A space really present in the line is not the segmentation's space.

    ``尺有所短 寸有所长`` tokenises to a run in which 短 and 寸 are adjacent,
    so the phrase query finds it for ``短寸``. There is no literal ``短寸`` in
    the line, and the filter has to say so — a filter that de-spaced
    ``line.folded`` first would erase the distinction and report a match that
    is not in the text.
    """
    assert line_ids(mini_db, "短寸"), "the phrase query should reach it"
    assert exact_line_ids(mini_db, "短寸") == set()
    # ...while a run that really is contiguous still matches
    assert len(exact_line_ids(mini_db, "寸有所长")) == 1


# --------------------------------------------------------------------------
# the mapping declarations, seen from the database
# --------------------------------------------------------------------------

def test_dynasty_rules_split_one_dataset_in_two(mini_db):
    assert mini_db.one(
        "SELECT dynasty FROM work WHERE external_id = 't-1'") == "tang"
    assert mini_db.one(
        "SELECT dynasty FROM work WHERE external_id = 's-1'") == "song"


def test_dataset_default_dynasty_applies(mini_db):
    assert mini_db.one(
        "SELECT dynasty FROM work WHERE title = '离骚'") == "xianqin"


def test_content_as_a_string_is_one_line(mini_db):
    """幽梦影's body is a single str; it must not be exploded per character."""
    rows = mini_db.rows(
        "SELECT line.idx, line.text FROM line JOIN work ON work.id = line.work_id "
        "WHERE work.title IS NULL ORDER BY line.idx")
    assert rows == [(0, "春风解冻，和气消冰")]


def test_content_as_a_list_is_one_line_per_element(mini_db):
    rows = mini_db.rows(
        "SELECT line.text FROM line JOIN work ON work.id = line.work_id "
        "WHERE work.title = '离骚' ORDER BY line.idx")
    assert rows == [("帝高阳之苗裔兮",), ("朕皇考曰伯庸",)]


def test_default_author_fills_a_missing_field(mini_db):
    assert mini_db.one(
        "SELECT author_name FROM work WHERE title IS NULL") == "张潮"


def test_null_external_id_is_accepted(mini_db):
    assert mini_db.one(
        "SELECT count(*) FROM work WHERE title = '无题' AND external_id IS NULL") == 1


def test_tags_survive_as_folded_json(mini_db):
    raw = mini_db.one("SELECT tags FROM work WHERE external_id = 't-1'")
    assert raw == '["五言绝句"]'
    assert mini_db.one(
        "SELECT count(*) FROM work WHERE tags IS NULL") == MINI_WORKS - 1


# --------------------------------------------------------------------------
# authors
# --------------------------------------------------------------------------

def test_authors_are_keyed_on_name_and_dynasty(mini_db):
    assert mini_db.one(
        "SELECT work_count FROM author WHERE name = '李白' AND dynasty = 'tang'") == 2
    assert mini_db.one(
        "SELECT work_count FROM author WHERE name = '王建' AND dynasty = 'tang'") == 1
    assert mini_db.one(
        "SELECT count(*) FROM author WHERE name = '李白'") == 1


def test_name_folded_is_the_segmented_name(mini_db):
    for name, folded in mini_db.rows("SELECT name, name_folded FROM author"):
        assert folded == folding.segment(name)


def test_authors_start_as_stubs(mini_db):
    """Phase 2 creates the row; phase 4 fills the biography in."""
    assert mini_db.one("SELECT count(*) FROM author WHERE bio IS NOT NULL") == 0
    assert mini_db.one("SELECT count(*) FROM author WHERE name_folded = ''") == 0


def test_author_fts_is_searchable_by_name(mini_db):
    hits = mini_db.rows(
        "SELECT author.name FROM author_fts JOIN author ON author.id = author_fts.rowid "
        "WHERE author_fts MATCH ?", folding.fts_query("李白"))
    assert [row[0] for row in hits] == ["李白"]


def test_work_fts_is_in_the_same_character_space(mini_db):
    """Titles are indexed from the folded shadow column, like the lines are."""
    for query in ("春風", "春风"):
        assert {row[0] for row in mini_db.rows(
            "SELECT work.title FROM work_fts JOIN work ON work.id = work_fts.rowid "
            "WHERE work_fts MATCH ?", folding.fts_query(query))} == {"春风"}


# --------------------------------------------------------------------------
# the deferred phase-2 tables exist so that phase 2 needs no migration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("table", ["strain", "unmatched_author", "author_match_stat",
                                   "author_alias"])
def test_deferred_tables_are_present_and_empty(mini_db, table):
    assert mini_db.one("SELECT count(*) FROM %s" % table) == 0
