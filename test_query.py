# -*- coding: utf-8 -*-
"""``loader.query``: first the synthetic corpus, then the real one.

The fixture tests fix the behaviour — one result per poem, the lines that
matched inside it, the folding equivalence, and the ``exact`` escape hatch
pinned against a Python substring test rather than against the SQL it is
supposed to be testing. The ``slow`` tests at the bottom are the ones a reader
actually cares about: they ask the 450 MB database the questions the plan set
as this phase's acceptance criteria.
"""
import pytest

from loader import folding, query

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@pytest.fixture
def conn(mini_db):
    """A query connection to the built miniature."""
    handle = query.connect(mini_db.db)
    yield handle
    handle.close()


def texts_of(conn, ids):
    """The display text of a set of line ids, as a set."""
    return {conn.execute("SELECT text FROM line WHERE id = ?", (line_id,))
            .fetchone()[0] for line_id in sorted(ids)}


def titles_of(hits):
    return [hit.work.title for hit in hits]


def literal_matches(conn, text):
    """Every matching line whose **display text** really contains ``text``.

    The oracle for ``exact=True``, and deliberately not a query: it takes the
    rows the phrase search returned and tests the substring in Python. An
    oracle written in SQL against ``line.text`` would share the expression it
    is checking, and would agree with a bug in it.
    """
    folded = folding.fold(text)
    loose = query.matching_line_ids(conn, text)
    if not loose:
        return set()
    ids = sorted(loose)
    rows = []
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        rows.extend(conn.execute(
            "SELECT id, text FROM line WHERE id IN (%s)"
            % ",".join("?" * len(batch)), batch).fetchall())
    assert len(rows) == len(ids), "the oracle lost rows between the two queries"
    return {row[0] for row in rows if folded in row[1]}


# --------------------------------------------------------------------------
# search: what a hit is
# --------------------------------------------------------------------------

def test_two_matching_lines_in_one_poem_are_one_hit(conn):
    """床前明月光 and 举头望明月 are two matching lines of one 静夜思.

    A hit carries the lines that matched and the work they belong to, so
    neither a per-line result nor a per-work result would do.
    """
    hits = [hit for hit in query.search(conn, "明月") if hit.work.title == "静夜思"]
    assert len(hits) == 1
    assert [line.text for line in hits[0].lines] == ["床前明月光", "举头望明月"]
    assert [line.idx for line in hits[0].lines] == [0, 2]
    assert hits[0].editions == ()


def test_a_hit_says_how_long_the_poem_is_not_how_much_matched(conn):
    """``lines`` is what matched; ``work.line_count`` is the whole poem."""
    hit = next(hit for hit in query.search(conn, "明月") if hit.work.title == "静夜思")
    assert len(hit.lines) == 2
    assert hit.work.line_count == 3


def test_a_hit_carries_the_work_it_came_from(conn):
    hit = next(hit for hit in query.search(conn, "春风") if hit.work.title == "春风")
    work = hit.work
    assert work.author == "李白"          # author.name, the canonical spelling
    assert work.author_name == "李白"     # the spelling this dataset publishes
    assert work.dataset == "tang"
    assert work.dynasty == "tang"
    assert work.kind == "shi"
    assert work.external_id == "t-1"
    assert work.path == "trad/poet.tang.0.json"
    assert work.line_count == 2


def test_lines_come_back_simplified(conn):
    """The database stores folded text and the query layer adds nothing back."""
    texts = texts_of(conn, query.matching_line_ids(conn, "春風"))
    assert texts == {"春风不相识", "春风解冻，和气消冰"}
    assert all(folding.fold(text) == text for text in texts)


def test_traditional_and_simplified_are_one_query(conn):
    """The acceptance criterion of the whole folding layer, on the fixture."""
    assert query.matching_line_ids(conn, "春风") == query.matching_line_ids(conn, "春風")
    assert query.matching_line_ids(conn, "西樓") == query.matching_line_ids(conn, "西楼")
    assert query.search(conn, "春風") == query.search(conn, "春风")
    assert query.count(conn, "春風") == query.count(conn, "春风")


# --------------------------------------------------------------------------
# search: the exact=True escape hatch
# --------------------------------------------------------------------------

def test_exact_is_the_literal_it_was_given(conn):
    """``exact`` agrees with a Python substring test over the whole hit set.

    The fixture's 夜坐 line is 夜气清而明，月照空山: the phrase query finds it
    because unicode61 drops the comma, and the literal test must not.
    """
    for text in ("明月", "明，月", "春风", "月", "秦時明月漢時關", "不识"):
        assert query.matching_line_ids(conn, text, exact=True) == \
            literal_matches(conn, text), text


def test_a_single_character_query_has_no_clause_to_break(conn):
    """The exact filter is a filter, not a second query: it never adds rows."""
    assert query.matching_line_ids(conn, "月", exact=True) == \
        query.matching_line_ids(conn, "月")


def test_exact_is_a_subset_and_the_difference_is_clause_breaks(conn):
    loose = query.matching_line_ids(conn, "明月")
    exact = query.matching_line_ids(conn, "明月", exact=True)
    assert exact < loose
    assert texts_of(conn, loose - exact) == {"夜气清而明，月照空山"}


def test_exact_does_not_invent_a_match_across_a_space(conn):
    """A space really in the line is not the segmentation's space.

    The segmentation puts a space between every pair of Han characters, so a
    filter that de-spaced ``line.folded`` — or that compared against that
    column at all — would claim 尺有所短 寸有所长 contains 短寸, which the
    phrase query does find and which the text does not contain.
    """
    assert query.matching_line_ids(conn, "短寸"), "the phrase query should reach it"
    assert query.matching_line_ids(conn, "短寸", exact=True) == set()
    # ...while the same two characters separated by the space the line really
    # has are a literal the line contains, and are found.
    assert texts_of(conn, query.matching_line_ids(conn, "短 寸", exact=True)) == {
        "尺有所短 寸有所长"}


def test_exact_needle_escapes_what_like_would_read_as_a_wildcard(conn):
    """A reader searching for 月% is searching for two characters, not a prefix."""
    assert query.exact_needle("月%") == "%月\\%%"
    assert query.exact_needle("100%") == "%100\\%%"
    assert query.exact_needle("a_b") == "%a\\_b%"
    # The backslash goes first, so an input that is itself a backslash cannot
    # escape the escape character.
    assert query.exact_needle("\\") == "%\\\\%"
    # End to end: the phrase query reaches every line with a 月 in it, and the
    # literal finds none of them, because no line contains a 月%.
    assert query.matching_line_ids(conn, "月%")
    assert query.matching_line_ids(conn, "月%", exact=True) == set()


# --------------------------------------------------------------------------
# search: grouping the works that are one poem
# --------------------------------------------------------------------------

def test_the_same_body_under_two_titles_is_one_hit(conn):
    """The fixture files 秦时明月汉时关 twice; a search returns it once.

    Its two copies are 橫吹曲辭 出塞 and 出塞二首 其一, one dataset and one file
    apart, which is how 全唐诗 files 出塞 up to the file boundary.
    """
    hits = query.search(conn, "秦时明月")
    assert titles_of(hits) == ["横吹曲辞 出塞"]
    assert [line.text for line in hits[0].lines] == ["秦时明月汉时关"]
    assert hits[0].also_titles == (("tang", "出塞二首 其一"),)
    edition = hits[0].editions[0]
    assert edition.external_id != hits[0].work.external_id
    assert edition.ordinal != hits[0].work.ordinal


def test_group_false_shows_every_copy(conn):
    """Grouping is a grouping, not a deletion: the copies are all still there."""
    grouped = query.search(conn, "秦时明月")
    every = query.search(conn, "秦时明月", group=False)
    assert len(every) == len(grouped) + 1
    assert sorted(titles_of(every)) == ["出塞二首 其一", "横吹曲辞 出塞"]
    assert all(hit.editions == () for hit in every)


def test_the_survivor_is_the_earliest_of_the_group(conn):
    """The kept copy is the lowest work id, so a page boundary cannot change it.

    A group's survivor decides what a page shows; if it depended on which page
    was read, paging would show the same poem twice and hide another.
    """
    hit = query.search(conn, "秦时明月")[0]
    assert hit.editions
    assert hit.work.id < min(other.id for other in hit.editions)


def test_grouping_merges_bodies_and_nothing_else(conn):
    """Only the duplicated pair collapses; the neighbours sharing 月 survive.

    静夜思, 夜坐 and 相见欢 all match 月 and each has a body of its own.
    """
    hits = query.search(conn, "月")
    assert {hit.work.title for hit in hits} == {
        "静夜思", "夜坐", "相见欢", "横吹曲辞 出塞"}
    collapsed = [hit for hit in hits if hit.editions]
    assert [hit.work.title for hit in collapsed] == ["横吹曲辞 出塞"]


# --------------------------------------------------------------------------
# search: filters, counts, paging
# --------------------------------------------------------------------------

def test_filters(conn):
    tang = query.search(conn, "月", dataset="tang")
    assert tang and {hit.work.dataset for hit in tang} == {"tang"}
    song = query.search(conn, "月", dynasty="song")
    assert song and {hit.work.dynasty for hit in song} == {"song"}
    assert titles_of(query.search(conn, "月", author="李煜")) == ["相见欢"]
    # The author filter folds too, so a traditional spelling finds the same
    # works as the Simplified one.
    assert query.search(conn, "月", author="王昌齡") == query.search(conn, "月", author="王昌龄")
    assert query.matching_line_ids(conn, "月", author="不存在的作者") == set()


def test_filters_are_and_not_or(conn):
    """A work has to satisfy every filter, not any one of them.

    相見歡 is filed in dataset tang and carries dynasty song — the fixture's
    dynasty_rules split one dataset in two — so the pair is a real query and
    the dynasty alone is not enough.
    """
    assert query.matching_line_ids(conn, "月", dynasty="song", dataset="tang") == \
        query.matching_line_ids(conn, "月", author="李煜")
    assert query.count(conn, "月", dynasty="song", dataset="qing") == (0, 0)


def test_count_agrees_with_the_search_it_counts(conn):
    for text in ("月", "明月", "春风", "不存在"):
        lines, works = query.count(conn, text)
        assert lines == len(query.matching_line_ids(conn, text)), text
        assert works == len(query.search(conn, text, group=False)), text


def test_count_of_a_grouped_result_is_the_number_of_bodies(conn):
    """``grouped_count`` is what a grouped page is a page of."""
    assert query.grouped_count(conn, "春风") == 2          # 春风, 春风解冻
    assert query.grouped_count(conn, "明月") == 3          # 静夜思, 出塞, 夜坐
    assert query.grouped_count(conn, "不存在") == 0
    lines, works = query.count(conn, "明月")
    assert (lines, works) == (5, 4)
    assert query.grouped_count(conn, "明月") < works


def test_paging_covers_the_set_without_gaps_or_repeats(conn):
    every = query.search(conn, "月", group=False)
    assert len(every) > 2
    pages = []
    for offset in range(0, len(every), 2):
        pages += query.search(conn, "月", group=False, limit=2, offset=offset)
    assert [hit.work.id for hit in pages] == [hit.work.id for hit in every]


def test_a_limit_without_grouping_returns_the_front_of_the_set(conn):
    """Without grouping a hit is one work, so the window is exactly the page.

    Which is to say: no heuristic is involved here, and the page is the rows a
    full scan's front slice would give.
    """
    every = query.search(conn, "月", group=False)
    assert query.search(conn, "月", group=False, limit=2) == every[:2]


def test_a_grouped_page_is_the_front_of_the_grouped_set(conn):
    """A windowed scan may report fewer editions than a full scan would.

    It must never change which poem is shown, or in what order: a group's
    survivor is its lowest work id, so a window that reaches a group at all
    reaches its survivor first.
    """
    every = query.search(conn, "月")
    assert query.search(conn, "月", limit=2) == every[:2]
    assert query.search(conn, "月", limit=1, offset=1) == every[1:2]


# --------------------------------------------------------------------------
# single records
# --------------------------------------------------------------------------

def test_work_by_id(conn):
    work_id = conn.execute("SELECT id FROM work WHERE title = '夜坐'").fetchone()[0]
    hit = query.work(conn, work_id)
    assert hit.work.title == "夜坐"
    assert [line.idx for line in hit.lines] == [0]
    assert hit.lines[0].text == "夜气清而明，月照空山"
    assert hit.editions == ()


def test_work_finds_its_editions(conn):
    """Asked about one copy, ``work`` names the others.

    This is the "why do I keep seeing this one twice" question, answered
    without a search — and the reason ``work`` is worth a second lookup.
    """
    first = conn.execute("SELECT id FROM work WHERE external_id = 't-4'").fetchone()[0]
    assert [other.external_id for other in query.work(conn, first).editions] == ["t-5"]
    # ...and symmetrically, from either copy.
    second = conn.execute("SELECT id FROM work WHERE external_id = 't-5'").fetchone()[0]
    assert [other.external_id for other in query.work(conn, second).editions] == ["t-4"]


def test_work_editions_are_not_the_works_that_merely_share_a_line(conn):
    """静夜思 also contains 秦时明月汉时关's opening character; it is not an edition."""
    work_id = conn.execute("SELECT id FROM work WHERE title = '静夜思'").fetchone()[0]
    assert query.work(conn, work_id).editions == ()


def test_work_of_a_missing_id_is_none(conn):
    assert query.work(conn, 999999) is None


def test_author_lookup(conn):
    found = query.author(conn, "李白")
    assert len(found) == 1
    assert found[0].name == "李白" and found[0].dynasty == "tang"
    assert found[0].work_count == 2
    assert query.author(conn, "不存在") == []
    assert query.author(conn, "李白", dynasty="song") == []


def test_author_lookup_folds_the_name(conn):
    """A traditional spelling finds the Simplified row."""
    found = query.author(conn, "無名氏")
    assert found == query.author(conn, "无名氏")
    assert [person.name for person in found] == ["无名氏"]


def test_author_works(conn):
    person = query.author(conn, "李白")[0]
    assert [work.title for work in query.author_works(conn, person.id)] == \
        ["春风", "静夜思"]
    assert query.author_works(conn, 999999) == []
    assert len(query.author_works(conn, person.id, limit=1)) == 1
    assert [work.title for work in query.author_works(conn, person.id, offset=1)] == \
        ["静夜思"]


# --------------------------------------------------------------------------
# the connection
# --------------------------------------------------------------------------

def test_connect_refuses_to_write(conn):
    """``PRAGMA query_only``: a bug in this module cannot damage a build.

    Which matters because the database it opens is 450 MB and a rebuild takes
    minutes on this disk.
    """
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM line")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO dataset(key, name) VALUES ('x', 'x')")
    assert conn.execute("SELECT count(*) FROM line").fetchone()[0] > 0


def test_connect_names_the_builder_when_the_database_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError) as caught:
        query.connect(str(tmp_path / "no.db"))
    assert "build_db" in str(caught.value)


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------

def run(capsys, mini_db, *argv):
    code = query.main(["--db", mini_db.db] + list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_search(capsys, mini_db):
    code, out, _ = run(capsys, mini_db, "--search", "春风")
    assert code == 0
    assert "春风不相识" in out
    assert "李白" in out
    assert "trad/poet.tang.0.json" in out


def test_cli_search_is_the_same_for_both_spellings(capsys, mini_db):
    """The header echoes what was typed; every line below it is the same.

    The echo is deliberate — a reader who typed the traditional spelling
    should see it back — so the equivalence is over the results, not the whole
    transcript.
    """
    code, simplified, _ = run(capsys, mini_db, "--search", "春风")
    traditional_code, traditional, _ = run(capsys, mini_db, "--search", "春風")
    assert (code, traditional_code) == (0, 0)
    assert simplified.splitlines()[1:] == traditional.splitlines()[1:]
    assert simplified.startswith("春风") and traditional.startswith("春風")


def test_cli_count(capsys, mini_db):
    code, out, _ = run(capsys, mini_db, "--search", "明月", "--count")
    assert code == 0
    assert "5 行 / 4 首" in out
    assert "合并相同正文后 3 篇" in out
    assert "床前明月光" not in out, "--count should print totals only"


def test_cli_exact(capsys, mini_db):
    _, loose, _ = run(capsys, mini_db, "--search", "明月", "--count")
    _, exact, _ = run(capsys, mini_db, "--search", "明月", "--exact", "--count")
    assert "5 行 / 4 首" in loose
    assert "4 行 / 3 首" in exact


def test_cli_all_expands_the_duplicate(capsys, mini_db):
    _, grouped, _ = run(capsys, mini_db, "--search", "秦时明月", "--count")
    _, every, _ = run(capsys, mini_db, "--search", "秦时明月", "--count", "--all")
    assert "2 行 / 2 首" in grouped
    assert "合并相同正文后 1 篇" in grouped
    assert "2 行 / 2 首" in every
    assert "合并" not in every


def test_cli_paging_says_how_much_is_left(capsys, mini_db):
    """The remainder is counted against the grouped total, not the raw one.

    Four bodies are shown a page of two at a time, though five works match;
    saying "还有 3 篇" here would be the lie this reports from two exact
    queries to avoid.
    """
    code, out, _ = run(capsys, mini_db, "--search", "月", "--limit", "2")
    assert code == 0
    assert "显示 1-2 / 4 篇" in out
    assert "还有 2 篇，用 --offset 2 继续" in out


def test_cli_work(capsys, mini_db):
    work_id = mini_db.one("SELECT id FROM work WHERE external_id = 't-4'")
    code, out, _ = run(capsys, mini_db, "--work", str(work_id))
    assert code == 0
    assert "横吹曲辞 出塞" in out
    assert "秦时明月汉时关" in out
    assert "出塞二首 其一" in out          # named as an edition


def test_cli_work_that_does_not_exist(capsys, mini_db):
    code, out, err = run(capsys, mini_db, "--work", "999999")
    assert code == 1
    assert "999999" in err


def test_cli_author(capsys, mini_db):
    code, out, _ = run(capsys, mini_db, "--author", "李白")
    assert code == 0
    assert "李白  →  1 位" in out
    assert "作品 2" in out
    assert "用 --author-id" in out


def test_cli_author_id(capsys, mini_db):
    author_id = mini_db.one("SELECT id FROM author WHERE name = '李白'")
    code, out, _ = run(capsys, mini_db, "--author-id", str(author_id))
    assert code == 0
    assert "静夜思" in out
    assert "春风" in out


def test_cli_author_as_a_filter(capsys, mini_db):
    """--author restricts a search when there is one, and looks a name up when
    there is not."""
    _, out, _ = run(capsys, mini_db, "--search", "月", "--author", "李煜")
    assert "相见欢" in out and "静夜思" not in out


def test_cli_reports_a_query_with_nothing_to_search_for(capsys, mini_db):
    """``，，`` folds and segments to nothing: a usage error, not a crash."""
    code, out, err = run(capsys, mini_db, "--search", "，，")
    assert code == 2
    assert out == ""
    assert "indexable" in err


def test_cli_requires_a_mode(capsys, mini_db):
    with pytest.raises(SystemExit):
        run(capsys, mini_db)


# --------------------------------------------------------------------------
# the real corpus
# --------------------------------------------------------------------------
# The plan's acceptance criteria, asked of the shipped database rather than of
# a fixture. They need the 450 MB build to exist; the queries themselves are
# milliseconds.

@pytest.fixture(scope="module")
def real():
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "build", "poetry.db")
    if not os.path.exists(path):
        pytest.skip("build/poetry.db has not been built")
    handle = query.connect(path)
    yield handle
    handle.close()


@pytest.mark.slow
def test_real_search_folds_both_spellings(real):
    """--search 春风 and --search 春風 return the same set, over the corpus.

    The phase's headline acceptance criterion, and the answer to the defect the
    plan opened with: searching 春风 in 全唐诗, which is 99.2% traditional,
    returned nothing at all before folding.
    """
    for simplified, traditional in (("春风", "春風"), ("秋风", "秋風"),
                                    ("万里", "萬里"), ("飞鸟", "飛鳥"),
                                    ("长安", "長安"), ("国家", "國家")):
        found = query.matching_line_ids(real, simplified)
        assert found, simplified
        assert found == query.matching_line_ids(real, traditional), simplified


@pytest.mark.slow
def test_real_exact_filter_is_the_literal(real):
    """``exact`` equals a Python substring test over the real match set.

    Measured on this build: 明月 is 5,788 lines loose and 5,779 exact over the
    whole database, and 4,199 / 4,193 inside 全唐诗全宋诗 — the six rows the
    filter removes are the ones where a comma is the only thing between the two
    characters. The plan's acceptance figure for the same pair was 4,184 /
    4,178, a difference of the same six; only the base differs, and the base is
    a property of how the scope was measured during exploration rather than of
    the filter. The invariant below is what the filter promises, so it is what
    this test asserts.
    """
    loose = query.matching_line_ids(real, "明月")
    exact = query.matching_line_ids(real, "明月", exact=True)
    assert exact < loose
    assert len(loose) - len(exact) >= 1, "the escape hatch would be vacuous"
    assert exact == literal_matches(real, "明月")


@pytest.mark.slow
def test_real_one_name_many_people(real):
    """王建 is a Tang poet and a Song poet; the schema keeps them apart.

    The plan's second acceptance criterion, and the reason ``author`` returns a
    list: a lookup that returned one row would have to choose, and choosing
    would merge two people.
    """
    found = query.author(real, "王建")
    assert len(found) >= 2
    assert len({person.dynasty for person in found}) == len(found)
    assert max(person.work_count for person in found) > 900


@pytest.mark.slow
def test_real_duplicate_bodies_collapse(real):
    """The duplicate works are grouped — not duplicated, not deleted.

    A grouped result is smaller than the ungrouped one in both works and lines,
    and smaller by exactly the copies it folded away: the lines of a collapsed
    edition go with the edition, so the totals a grouped page reports describe
    the page's own works. ``count`` is the ungrouped measure, and it is what
    ``group=False`` returns.
    """
    lines, works = query.count(real, "明月")
    groups = query.grouped_count(real, "明月")
    assert groups < works, "the corpus is known to hold duplicate bodies"

    every = query.search(real, "明月", group=False, limit=None)
    assert len(every) == works
    assert sum(len(hit.lines) for hit in every) == lines

    grouped = query.search(real, "明月", limit=None)
    assert len(grouped) == groups
    assert sum(len(hit.lines) for hit in grouped) < lines


@pytest.mark.slow
def test_real_a_hit_never_lists_itself(real):
    for hit in query.search(real, "出塞", limit=None):
        assert hit.work.id not in {other.id for other in hit.editions}


@pytest.mark.slow
def test_real_every_work_has_an_author(real):
    """"作者不详" is a query, not a NULL: no work is missing an author row."""
    assert real.execute(
        "SELECT count(*) FROM work WHERE author_id IS NULL").fetchone()[0] == 0
    assert real.execute(
        "SELECT count(*) FROM work WHERE author_id NOT IN "
        "(SELECT id FROM author)").fetchone()[0] == 0


@pytest.mark.slow
def test_real_the_ming_reading_of_jingyesi_is_not_in_this_corpus(real):
    """床前明月光 finds nothing, and that is the corpus rather than the index.

    This corpus carries the Song textual variant — 床前看月光 … 舉頭望山月 — so
    the Ming reading a reader is most likely to type is genuinely absent. The
    test is here so that the day it *is* found, that stops being a surprising
    zero and becomes a change someone made on purpose.
    """
    assert query.matching_line_ids(real, "床前明月光") == set()
    assert query.matching_line_ids(real, "床前看月光")
    assert query.matching_line_ids(real, "举头望山月")
