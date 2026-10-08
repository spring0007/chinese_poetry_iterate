# -*- coding: utf-8 -*-
"""The biography pass: loader/authors.py.

Everything behavioural runs on the synthetic corpus, extended here with a
handful of records and a biography file so that each rung of the match ladder
fires exactly once and the expected counts can be written down by hand. The
real corpus is 345 MB and its biography files are read only by the two tests
marked ``slow`` at the bottom.

The fixture's numbers are load-bearing. Adding a record means changing them:

    works 12   authors 9
    exact        2 authors   3 works   李白 tang, 王建 tang
    alias        1 author    1 work    李太白 tang -> 李白
    dynasty_gap  1 author    2 works   屈原 xianqin <- biography filed as song
    stub         5 authors   6 works   无名氏 李煜 张潮 王昌龄 王建(song)
"""
import json
import os
import sqlite3

import pytest

from loader import authors, folding

#: Extra corpus records the fixture adds on top of conftest's MINI_FILES. Two
#: more 王建s would be a different person's problem; the point of each is stated.
EXTRA_FILES = {
    # 王建 under a second dynasty. The biography file has 王建 filed as tang,
    # so the song row must be left alone and reported -- this is the plan's
    # "王建 解析出多个不同作者", and merging them would be the exact silent
    # error UNIQUE(name, dynasty) exists to prevent.
    "trad/poet.song.1.json": [
        {"id": "s-2", "title": "宮詞", "author": "王建",
         "paragraphs": ["樹頭樹底覓殘紅"]},
    ],
    # A spelling no amount of folding will join to 李白 (fold leaves both
    # standing), so it is what the hand-written alias table is for.
    "trad/poet.tang.2.json": [
        {"id": "t-6", "title": "贈汪倫", "author": "李太白",
         "paragraphs": ["李白乘舟將欲行"]},
    ],
}

TANG_BIOS = [
    ({"name": "李白", "desc": "李白，字太白，隴西成紀人。少有逸才。"}, "exact"),
    ({"name": "王建", "desc": "王建，字仲初，潁川人。大曆十年進士。"}, "exact"),
    # This corpus' truncation marker. 宋词/author.song.json has 1,538 like it.
    ({"name": "無名氏", "desc": "--"}, "dropped"),
    # Nobody in the corpus. A biography is not evidence that an author exists;
    # importing it would invent an author row with no works.
    ({"name": "某某某", "desc": "某某某，無此人。"}, "bio_only"),
    ({"name": "", "desc": "無名"}, "dropped"),
    ({"name": "李煜", "desc": ""}, "dropped"),
]

SONG_BIOS = [
    # 屈原 is in the corpus as xianqin and his name occurs exactly once in the
    # author table, so the unique-name rung may attach a biography filed under
    # another dynasty -- and must record that it did.
    ({"name": "屈原", "desc": "屈原，名平，楚之同姓也。為三閭大夫。"}, "gap"),
]

#: Both sides are written in the database's own spelling -- the table is not
#: folded on the way in, because folding it a second time moves the key (see
#: loader.author_aliases.json). So the key here is 张潮, not 張潮: the corpus
#: spells it 张潮 and fold() runs on the way into the database, not here.
ALIASES = {
    "aliases": {
        "李太白": {"dynasty": "tang", "name": "李白",
                   "why": "fixture: same person, two spellings"},
        # An alias whose canonical spelling has no biography must not invent
        # one; it falls through to the report like any other unresolved name.
        "张潮": {"dynasty": "qing", "name": "张潮", "why": "fixture: no bio"},
    }
}


@pytest.fixture
def bio_db(mini_corpus):
    """The mini corpus plus a biography file, built."""
    for relative, records in EXTRA_FILES.items():
        path = os.path.join(mini_corpus.root, relative)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False)

    os.makedirs(os.path.join(mini_corpus.root, "bios"), exist_ok=True)
    for filename, records in (("authors.tang.json", TANG_BIOS),
                              ("authors.song.json", SONG_BIOS)):
        with open(os.path.join(mini_corpus.root, "bios", filename), "w",
                  encoding="utf-8") as handle:
            json.dump([r for r, _ in records], handle, ensure_ascii=False)

    aliases = os.path.join(mini_corpus.root, "aliases.json")
    with open(aliases, "w", encoding="utf-8") as handle:
        json.dump(ALIASES, handle, ensure_ascii=False)

    with open(mini_corpus.config, encoding="utf-8") as handle:
        config = json.load(handle)
    config["biographies"] = {
        "test.tang": {"name": "fixture 唐", "path": "bios/authors.tang.json",
                      "name_key": "name", "bio_key": "desc", "dynasty": "tang"},
        "test.song": {"name": "fixture 宋", "path": "bios/authors.song.json",
                      "name_key": "name", "bio_key": "desc", "dynasty": "song"},
    }
    with open(mini_corpus.config, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False)

    mini_corpus.build(aliases_path=aliases)
    return mini_corpus


def rungs(corpus):
    return {rung: (a, w) for rung, a, w in corpus.rows(
        "SELECT rung, authors, works FROM author_match_stat")}


def test_every_work_has_an_author(bio_db):
    """"作者不详" is a stub row, not a NULL -- so no query site checks NULL."""
    assert bio_db.one("SELECT count(*) FROM work WHERE author_id IS NULL") == 0
    assert bio_db.one("SELECT count(*) FROM work") == 12


def test_ladder_counts(bio_db):
    """Each rung fires exactly once, and the works add up to the corpus."""
    assert rungs(bio_db) == {
        "exact": (2, 3),
        "alias": (1, 1),
        "dynasty_gap": (1, 2),
        "stub": (5, 6),
    }
    total = bio_db.one("SELECT sum(works) FROM author_match_stat")
    assert total == bio_db.one("SELECT count(*) FROM work")


def test_author_names_are_unique_per_dynasty(bio_db):
    assert bio_db.one(
        "SELECT count(*) FROM (SELECT name, dynasty FROM author "
        "GROUP BY name, dynasty HAVING count(*) > 1)") == 0
    assert bio_db.one("SELECT count(*) FROM author") == 9


def test_one_name_can_be_two_people(bio_db):
    """王建 is a Tang poet and a Song poet. Both resolve; only tang has a bio."""
    from loader import query

    conn = bio_db.connect()
    try:
        rows = query.author(conn, "王建")
    finally:
        conn.close()
    assert len(rows) == 2
    assert {r.dynasty for r in rows} == {"tang", "song"}
    assert [r.bio is not None for r in rows].count(True) == 1
    assert {r.dynasty: r.bio_source for r in rows}["song"] == "missing"


def test_ambiguous_cross_dynasty_name_is_reported_not_merged(bio_db):
    """A biography under another dynasty is reported, never copied across.

    The 45 names in the real corpus that look like this are the same shape:
    their biography is already attached to the row under its own dynasty, and
    joining the other row to it is the cross-dynasty merge the schema forbids.
    """
    causes = dict(bio_db.rows(
        "SELECT name, cause FROM unmatched_author WHERE name = '王建'"))
    assert causes == {"王建": "ambiguous"}
    # and the biography really is on the tang row, not the song one
    assert bio_db.one(
        "SELECT bio FROM author WHERE name = '王建' AND dynasty = 'tang'") is not None


def test_alias_attaches_without_moving_works(bio_db):
    """Rung 2 copies the biography and leaves the works where they are.

    A second spelling that folds to the same name is a search no-op, so
    re-pointing the works would buy nothing and risk colliding with the
    UNIQUE(name, dynasty) key.
    """
    assert bio_db.one(
        "SELECT bio FROM author WHERE bio_source = 'test.tang' "
        "AND name = '李太白'") is not None
    assert bio_db.one(
        "SELECT count(*) FROM work w JOIN author a ON a.id = w.author_id "
        "WHERE a.name = '李太白'") == 1
    alias, dynasty, canonical = bio_db.rows(
        "SELECT al.alias, al.dynasty, a.name FROM author_alias al "
        "JOIN author a ON a.id = al.author_id")[0]
    assert (alias, dynasty, canonical) == ("李太白", "tang", "李白")


def test_alias_without_a_biography_invents_nothing(bio_db):
    """张潮 -> 张潮 is declared, but 张潮 has no biography, so rung 2 must not
    fire: an alias cannot conjure a biography the sources do not hold. The key
    does match its author row -- otherwise this would pass for the wrong
    reason and stop testing the rule it names."""
    assert bio_db.one(
        "SELECT count(*) FROM author WHERE name = '张潮' AND dynasty = 'qing'") == 1
    assert bio_db.one(
        "SELECT count(*) FROM author_alias al JOIN author a ON a.id = al.author_id "
        "WHERE al.alias = '张潮'") == 0
    assert dict(bio_db.rows(
        "SELECT name, cause FROM unmatched_author WHERE name = '张潮'")) == {
        "张潮": "no_bio"}


def test_dynasty_gap_is_attached_and_recorded(bio_db):
    """Rung 3 is the only rung that reasons across a boundary, so it leaves a
    trace even though it succeeded."""
    assert bio_db.one(
        "SELECT bio_source FROM author WHERE name = '屈原'") == "test.song"
    assert bio_db.one(
        "SELECT dynasty FROM author WHERE name = '屈原'") == "xianqin"
    # a reader who does not trust rung 3 can find everything it decided
    assert bio_db.rows(
        "SELECT name, cause, work_count FROM unmatched_author "
        "WHERE cause = 'dynasty_gap'") == [("屈原", "dynasty_gap", 2)]


def test_unresolved_is_reported_with_samples(bio_db):
    rows = dict(bio_db.rows(
        "SELECT name, cause FROM unmatched_author WHERE cause = 'no_bio'"))
    assert rows == {"无名氏": "no_bio", "李煜": "no_bio",
                    "张潮": "no_bio", "王昌龄": "no_bio"}
    samples = bio_db.one(
        "SELECT samples FROM unmatched_author WHERE name = '无名氏'")
    ids = json.loads(samples)
    assert 0 < len(ids) <= 3
    assert all(isinstance(i, int) for i in ids)


def test_bio_short_is_the_first_sentence(bio_db):
    assert bio_db.one(
        "SELECT bio_short FROM author WHERE name = '李白'") == "李白，字太白，陇西成纪人。"


def test_bio_folds_on_the_way_in(bio_db):
    """The biography sources are traditional and the database is not.

    The fixture's biography says 隴西成紀 and the row has to say 陇西成纪: bio
    is what a user reads and bio_folded is what author_fts indexes, and an
    unfolded one would leave this the only index in the database living in
    traditional space. ``segment`` alone would not catch it -- it spaces text
    out and never folds it.
    """
    bio = bio_db.one("SELECT bio FROM author WHERE name = '李白'")
    assert bio == "李白，字太白，陇西成纪人。少有逸才。"
    assert bio_db.one("SELECT bio_folded FROM author WHERE name = '李白'") == \
        folding.segment(bio)


def test_biography_is_searchable_in_simplified(bio_db):
    """The point of the fold: a simplified query reaches a traditional source.

    李太白 is in the answer as well as 李白, and that is rung 2 showing through:
    the alias copies the biography rather than moving the works, so the man is
    findable under both spellings and the index says so.
    """
    for query in ("陇西", "隴西"):
        found = bio_db.rows(
            "SELECT a.name FROM author_fts f JOIN author a ON a.id = f.rowid "
            "WHERE author_fts MATCH ? ORDER BY a.name", folding.fts_query(query))
        # Code-point order, not a ranking: 太 (U+592A) sorts before 白 (U+767D).
        assert [r[0] for r in found] == ["李太白", "李白"], query


def test_truncated_and_empty_biographies_are_dropped(bio_db):
    """The 1,538 '--' records and 1,108 empty descs in the real sources must
    not become biographies; the author stays a stub instead."""
    assert bio_db.one(
        "SELECT bio FROM author WHERE name = '无名氏'") is None
    assert bio_db.one(
        "SELECT bio FROM author WHERE name = '李煜'") is None


def test_biography_for_an_unknown_name_creates_no_author(bio_db):
    assert bio_db.one("SELECT count(*) FROM author WHERE name = '某某某'") == 0


def test_pass_is_idempotent(bio_db):
    """Running it twice changes nothing -- which is what makes it safe to call
    from an incremental build."""
    before = (bio_db.one("SELECT count(*) FROM author_match_stat"),
              bio_db.one("SELECT sum(works) FROM author_match_stat"),
              bio_db.one("SELECT count(*) FROM unmatched_author"),
              bio_db.one("SELECT count(*) FROM author WHERE bio IS NOT NULL"))

    from loader import build_db

    conn = build_db.connect(bio_db.db)
    try:
        with open(bio_db.config, encoding="utf-8") as handle:
            config = json.load(handle)
        authors.import_biographies(
            conn, config, repo_root=bio_db.root,
            aliases_path=os.path.join(bio_db.root, "aliases.json"))
    finally:
        conn.close()

    after = (bio_db.one("SELECT count(*) FROM author_match_stat"),
             bio_db.one("SELECT sum(works) FROM author_match_stat"),
             bio_db.one("SELECT count(*) FROM unmatched_author"),
             bio_db.one("SELECT count(*) FROM author WHERE bio IS NOT NULL"))
    assert before == after


def test_a_corpus_without_biographies_still_builds(mini_corpus):
    """The pass is optional: no 'biographies' block, no biography, no error."""
    mini_corpus.build()
    assert mini_corpus.one("SELECT count(*) FROM author_match_stat") == 0
    assert mini_corpus.one("SELECT count(*) FROM author WHERE bio IS NOT NULL") == 0
    assert mini_corpus.one("SELECT count(*) FROM author") == 7
    assert mini_corpus.one("SELECT count(*) FROM work WHERE author_id IS NULL") == 0


def test_first_sentence():
    assert authors.first_sentence("王建，字仲初。大曆十年進士。") == "王建，字仲初。"
    assert authors.first_sentence("無句讀") == "無句讀"
    assert authors.first_sentence("") == ""
    assert len(authors.first_sentence("字" * 500)) == authors._SHORT_LIMIT


def test_shipped_alias_table_parses():
    """The one measured pair where folding cannot join two spellings.

    If this ever grows past a handful of entries, someone has started guessing.
    Two others (朱庆余 → 朱庆馀, 349 works; 魏征 → 魏徵, 95 works) were removed
    on 2026-10-07: the biography source gained a simplified row for each, so
    both spellings merged upstream and rung 1 now takes every work. An entry
    the build routes around is not harmless — it keeps asserting a claim about
    the corpus that stopped being true, and only the slow sibling's exact
    count notices.
    """
    aliases = authors.load_aliases()
    assert aliases == {("李嘉佑", "tang"): "李嘉祐"}
    # Why the table is read verbatim and not folded: folding 朱庆馀 again moves
    # it to 朱庆余, which is a *different* author row, so an entry folded on the
    # way in would name a person nobody asked about and silently never fire.
    assert folding.fold("朱庆馀") == "朱庆余"


@pytest.mark.slow
def test_shipped_aliases_actually_fire(repo_root):
    """Every shipped alias names a row that exists and a biography that exists.

    This is the guard for the failure the table is most prone to: an alias that
    parses, looks maintained, and resolves nothing -- either because the key is
    not an author row or because the canonical spelling is not a biography key.
    """
    import json as _json

    db = os.path.join(repo_root, "build", "poetry.db")
    if not os.path.exists(db):
        pytest.skip("no built database")
    with open(os.path.join(repo_root, "loader", "datas.json"), encoding="utf-8") as f:
        config = _json.load(f)
    bios, _ = authors.collect(config["biographies"], repo_root)
    aliases = authors.load_aliases()

    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        rows = {(n, d) for n, d in conn.execute("SELECT name, dynasty FROM author")}
    finally:
        conn.close()
    for (alias, dynasty), canonical in aliases.items():
        assert (alias, dynasty) in rows, "no author row for %s/%s" % (alias, dynasty)
        assert (canonical, dynasty) in bios, \
            "%s/%s points at %s, which no biography source describes" % (
                alias, dynasty, canonical)


# --------------------------------------------------------------------------
# The real corpus
# --------------------------------------------------------------------------

@pytest.mark.slow
def test_real_corpus_biography_sources_exist(datas_config, repo_root):
    """Every declared biography source is a file whose declared keys are real."""
    for key, spec in datas_config["biographies"].items():
        path = os.path.join(repo_root, spec["path"])
        assert os.path.exists(path), key
        with open(path, encoding="utf-8") as handle:
            records = json.load(handle)
        assert records, key
        assert spec["name_key"] in records[0], key
        assert spec["bio_key"] in records[0], key


@pytest.mark.slow
def test_real_database_biography_coverage(repo_root):
    """The measured coverage, and the per-dynasty floor a regression must trip.

    A single aggregate threshold would hide most of a collapse: several whole
    dynasties (元曲's yuan, 纳兰性德's qing, 楚辞/诗经/论语's xianqin, 曹操's
    han) have no biography source at all and can never resolve, so the average
    is dragged down by a cause that is not a defect. Where a source exists,
    coverage is high, and that is what is asserted.
    """
    db = os.path.join(repo_root, "build", "poetry.db")
    if not os.path.exists(db):
        pytest.skip("no built database")
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        assert conn.execute(
            "SELECT count(*) FROM work WHERE author_id IS NULL").fetchone()[0] == 0
        stats = dict((r, (a, w)) for r, a, w in conn.execute(
            "SELECT rung, authors, works FROM author_match_stat"))
        resolved = sum(w for r, (_, w) in stats.items() if r != "stub")
        total = sum(w for _, w in stats.values())
        # 93.82% measured on the 2026-09-18 full build; floor rounded down.
        # The plan's 99.6% target was a pre-build estimate over 332,908 works,
        # i.e. a smaller denominator that did not yet include 元曲 (11,057 works,
        # most of them titled 高明《蔡伯喈琵琶记》 -- the play name sits in the
        # author field) or the other datasets that have no biography source at
        # all. It is a different fraction, not a missed target.
        assert resolved / total > 0.93, stats
        # The one shipped alias, and the exact number of works it is responsible for.
        # 李嘉佑 (134 works) is the alias rung: fold() does not map 祐->佑, so
        # the second spelling cannot reach the biography on its own.
        # Rung 2 is the one place a fold applied twice silently does nothing,
        # so the exact total -- not ">= 1" -- is the guard. Two entries that
        # used to be here (朱庆余, 魏征) were dropped on 2026-10-07 once the
        # biography source acquired a simplified row for each and rung 1 took
        # over; the number moving is the signal that they are really gone.
        assert stats["alias"][1] == 134, stats
        # 王建 is a Tang poet and a Song poet and 全唐诗 + 宋词 both describe him
        rows = conn.execute(
            "SELECT dynasty, bio IS NOT NULL FROM author WHERE name = '王建'"
        ).fetchall()
        assert len(rows) >= 2
        # Floors measured per dynasty, author-count weighted (the stricter of
        # the two weightings: song is 98.23% by works but 91.06% by author, so
        # author-count is what trips first). Only 全唐诗/全宋诗 (tang, song),
        # 宋词 (song) and 南唐 (wudai, 2 records) have sources; the rest are
        # asserted to have none, because "no source" must not silently become
        # "invented biography".
        # yuan's 1/233 is not a source either: it is 元好问, attached by rung 3
        # across the 金/元 boundary and recorded in author_match_stat as
        # dynasty_gap. Its floor is that single author, so a fold or alias
        # change that reaches 元曲 would still show up here.
        floors = {"song": 0.90, "tang": 0.65, "wudai": 0.09, "yuan": 0.004}
        sourceless = {"qing", "xianqin", "han"}
        seen = set()
        for dynasty, authors_, bios in conn.execute(
                "SELECT a.dynasty, count(DISTINCT a.id), "
                "       count(DISTINCT CASE WHEN a.bio IS NOT NULL THEN a.id END) "
                "FROM author a GROUP BY a.dynasty"):
            seen.add(dynasty)
            coverage = bios / authors_
            if dynasty in floors:
                assert coverage >= floors[dynasty], (dynasty, coverage, authors_)
            elif dynasty in sourceless:
                assert coverage == 0, (dynasty, coverage, bios)
        assert set(floors) | sourceless <= seen, seen
    finally:
        conn.close()
