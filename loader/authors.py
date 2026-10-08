# -*- coding: utf-8 -*-
"""Attach the biography files to the authors the build already created.

    python -m loader.authors --report
    python -m loader.authors --import

This is a **post-pass over a built database**, not a second loader. The build
already created one ``author`` row per distinct (folded name, dynasty) it met
while reading works, and every work points at one of them. All that is missing
is ``author.bio`` and its three companions. Filling them afterwards rather than
loading biographies first is not a matter of taste: the join key is folded on
both sides, so which rows exist cannot depend on whether the biographies went in
first. A post-pass is also independently runnable, testable and repeatable —
which is what an incremental build needs.

**The join key is ``author.name`` verbatim against ``fold(bio_name)`` — one fold
per side, never two.** ``author.name`` was already folded by ``loader.mapping``
on the way in, and folding it again is *not* a no-op: ``fold`` is not idempotent
on this corpus. zhconv maps 餘 to 馀 in one pass and 馀 to 余 in another, so
``fold('朱慶餘')`` is ``朱庆馀`` while ``fold(fold('朱慶餘'))`` is ``朱庆余``;
likewise ``fold('魏徵')`` is ``魏徵`` but ``fold('徵')`` is ``征``, because the
phrase table protects the full name. Re-folding a stored name therefore moves
the key and loses matches a single pass would have found. Measured on the real
corpus: double-folding drops 朱庆馀 (348 works) and reports 95.40% resolved,
single-folding finds it and reports 95.50%.

The ladder, and what each rung is worth on the real corpus:

============  ==========  =========  ==========================================
rung          authors     works      note
============  ==========  =========  ==========================================
``exact``     12,579      371,802    (folded name, dynasty) is in a source
``alias``     0           0          hand-maintained; empty in v1, see below
``dynasty_gap`` 1          4          唯一 name, biography under another dynasty
``stub``      1,271       17,537     no biography; recorded, never guessed
============  ==========  =========  ==========================================

95.50% of works carry a biography. The plan predicted 99.6%, and the gap is not
a defect in this code: 14,437 of the 17,537 unresolved works are in datasets
that have **no biography source at all** — 元曲 (11,057 works), 纳兰性德,
幽梦影, 楚辞, 诗经, 论语, 四书五经, 花间集, 曹操诗集. Everywhere a source exists
coverage is high: 全唐诗全宋诗 99.9%, 水墨唐诗 98.9%, 御定全唐詩 98.5%, 宋词 78.5%.

Rung 3 is nearly dead, and it is kept anyway because it is cheap and it is
*correct*: exactly one name qualifies (元好问, filed as 元 and biographied as
宋). The 37 names that look like rung-3 cases are something else — the corpus
itself files one poet under two dynasties (温庭筠 in 花间集 as wudai and in
全唐诗 as tang; 顾敻 wudai 54 + tang 56). Their biographies are already attached
to the tang row, and joining the wudai row to it would be the cross-dynasty
merge ``UNIQUE(name, dynasty)`` exists to forbid — the same decision that
correctly keeps 王建 tang and 王建 song apart.

**Rung 2 ships empty.** It is tempting to derive aliases mechanically from
one-character differences between an unresolved name and a biography name, and
it is wrong: restricted to a single dynasty it still pairs 程垓 with 程颢, 程颐
and forty other 程X, and 赵长卿 with both 沈长卿 and 胡长卿 — "recovering" 102,934
works of pure noise. Aliases are a claim about who is the same person; they are
written by hand in ``loader/author_aliases.json`` or not at all.

Nothing here rewrites ``author.name``. The biography files spell names in their
own way, but a folded name is already the meeting point, so rewriting it to a
second spelling that folds the same way would change no query and risk a
``UNIQUE(name, dynasty)`` collision. ``bio_source`` records which file the
biography came from; the rest of the row belongs to the works.

The biography *text*, unlike the name, does fold on the way in — for the same
reason the poems do. The sources are traditional and the database is
Simplified-only, and ``bio_folded`` is indexed, so an unfolded biography would
leave the author index in a different character space from ``line_fts``.
"""
import argparse
import collections
import json
import os
import sqlite3
import sys

from . import folding

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DATAS_CONFIG = os.path.join(HERE, "datas.json")
ALIASES_PATH = os.path.join(HERE, "author_aliases.json")

#: Causes recorded in ``unmatched_author``. ``dynasty_gap`` is the odd one out:
#: it marks a biography that *was* attached, across a dynasty boundary. It is
#: written down because rung 3 is the only rung that guesses, and a guess that
#: leaves no trace is indistinguishable from a fact.
NO_BIO = "no_bio"            # no biography source has this name at all
AMBIGUOUS = "ambiguous"      # a source has it, under some other dynasty
DYNASTY_GAP = "dynasty_gap"  # attached anyway: unique name, unique source

#: Where a biography's first sentence ends. Chinese full stop, exclamation,
#: question and semicolon, plus their ASCII twins and a newline.
_SENTENCE_ENDS = "。！？；!?;\n"

#: Longest ``bio_short`` produced when the biography has no sentence end at all.
_SHORT_LIMIT = 120


def first_sentence(text, limit=_SHORT_LIMIT):
    """The opening sentence of ``text``, for a one-line display.

    >>> first_sentence("王建，字仲初，颍川人。大历十年进士。")
    '王建，字仲初，颍川人。'

    A biography that never ends a sentence is returned whole unless it is
    longer than ``limit``, in which case it is cut — a plain prefix, not a
    fabricated ellipsis.
    """
    for i, ch in enumerate(text):
        if ch in _SENTENCE_ENDS:
            return text[:i + 1]
    return text[:limit]


def load_aliases(path=ALIASES_PATH):
    """Read ``loader/author_aliases.json`` into ``{(alias, dynasty): canonical}``.

    The file is shaped ``{"aliases": {"魏征": {"dynasty": "tang", "name": "魏徵"}}}``.
    A missing file is an empty table, which is the shipped state: see the module
    docstring for why it is not derived automatically.

    **Neither side is folded, and both sides are written in the database's own
    spelling.** Folding them would be the double-fold this module opens by
    warning about, and it would silently disable the entries: a canonical
    spelling that had been folded on the way in no longer equals the biography
    key it is supposed to name once folded a second time (朱慶餘 and 朱庆余
    both land on 朱庆余, so a table entry written against either intermediate
    form stops firing) — the rung would simply never fire, and the alias table
    would look maintained while doing nothing.
    ``test_shipped_alias_table_parses`` and its slow sibling check that every
    entry parses *and* fires.

    An entry that has become redundant should be deleted rather than left
    pointing at a name no source describes: two of the three originally
    shipped entries (朱庆余 → 朱庆馀, 魏征 → 魏徵) were dropped on 2026-10-07
    once the biography source acquired a simplified row for each and rung 1
    took over every work. A table whose entries can outlive their reason is
    worse than an empty one — ``load_aliases`` cannot tell a live entry from a
    dead one, and the only thing that notices is the exact alias-rung count
    ``test_real_database_biography_coverage`` asserts.
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    aliases = {}
    for alias, spec in (payload.get("aliases") or {}).items():
        name = spec.get("name") if isinstance(spec, dict) else spec
        dynasty = spec.get("dynasty") if isinstance(spec, dict) else None
        if not name:
            raise SystemExit("alias %r in %s has no 'name'" % (alias, path))
        if not dynasty:
            raise SystemExit("alias %r in %s has no 'dynasty'" % (alias, path))
        aliases[(alias, dynasty)] = name
    return aliases


def collect(sources, repo_root=REPO_ROOT):
    """Read every biography source into ``{(folded name, dynasty): (bio, source)}``.

    Both the name and the biography are folded on the way in. The name because
    it is the join key; the biography because the database is Simplified-only
    and the biography sources are not — 全唐诗/authors.tang.json is 99.2%
    traditional. Storing the biography as published would be invisible in
    ``bio`` and wrong in ``bio_folded``, which is indexed: ``segment`` spaces
    a string out but does not fold it, so the author index would be the one
    index in the database living in traditional space, and a search for 陇西
    would miss 隴西 while a search for 春風 matched 春风 in every poem.

    Three kinds of record are dropped, and counted so the number is visible
    rather than mysterious:

    * an empty name, or an empty biography;
    * a biography beginning ``--``, which is this corpus' truncation marker.
      1,538 records in ``宋词/author.song.json`` are shaped that way, 79 of
      them with a single-character name (``李`` eight times), so they carry no
      information about anyone.

    Where two sources supply the same (folded name, dynasty), the *longer*
    biography wins. The sources are near-duplicates rather than rivals — 全唐诗
    and 宋词 both describe 苏轼 — and the shorter text is a truncation of the
    longer, never a different account.
    """
    bios = {}
    dropped = collections.Counter()
    for key, spec in sorted(sources.items()):
        path = os.path.join(repo_root, spec["path"])
        with open(path, encoding="utf-8") as handle:
            records = json.load(handle)
        if not isinstance(records, list):
            raise SystemExit("biography source %s is not a list" % spec["path"])
        name_key = spec["name_key"]
        bio_key = spec["bio_key"]
        dynasty = spec["dynasty"]
        for record in records:
            if not isinstance(record, dict):
                dropped["not_a_record"] += 1
                continue
            name = (record.get(name_key) or "").strip()
            bio = (record.get(bio_key) or "").strip()
            if not name:
                dropped["no_name"] += 1
                continue
            if not bio:
                dropped["empty"] += 1
                continue
            if bio.startswith("--"):
                dropped["truncated"] += 1
                continue
            # The database is Simplified-only and the biography sources are
            # not, so the text folds here; ``segment(bio)`` downstream is then
            # spacing already-folded text, which is the one thing it does.
            name = folding.fold(name)
            bio = folding.fold(bio)
            match = (name, dynasty)
            known = bios.get(match)
            if known is None or len(bio) > len(known[0]):
                bios[match] = (bio, key)
    return bios, dropped


def import_biographies(conn, config, repo_root=REPO_ROOT, aliases_path=ALIASES_PATH):
    """Fill in every author's biography. Returns a stats dict.

    Idempotent: the pass owns ``bio``, ``bio_folded``, ``bio_short``,
    ``bio_source``, ``author_alias``, ``unmatched_author`` and
    ``author_match_stat`` outright, so it clears them and rewrites them rather
    than merging into whatever a previous run left. Running it twice on the same
    database produces the same rows, which is what makes it safe to call from
    the incremental build.
    """
    sources = config.get("biographies") or {}
    if not sources:
        return {"enabled": False, "rungs": {}, "unmatched": 0, "resolved": 0.0}
    bios, dropped = collect(sources, repo_root)
    aliases = load_aliases(aliases_path)

    # Folded names that *some* source can describe, whatever the dynasty. Used
    # only to tell "nobody wrote this person's life" (no_bio) from "somebody did,
    # under another dynasty, and we decline to guess" (ambiguous).
    bio_dynasties = collections.defaultdict(set)
    for folded, dynasty in bios:
        bio_dynasties[folded].add(dynasty)

    rows = conn.execute(
        "SELECT id, name, dynasty FROM author ORDER BY id"
    ).fetchall()
    works_by_id = dict(conn.execute(
        "SELECT author_id, count(*) FROM work GROUP BY author_id"))
    by_name = collections.defaultdict(list)
    ids_by_name = collections.defaultdict(dict)
    for author_id, name, dynasty in rows:
        by_name[name].append(author_id)
        ids_by_name[name][dynasty] = author_id

    # Decide every row's rung *before* writing anything. The alias rung reads
    # another row's outcome, so deciding and writing in one pass would make the
    # result depend on id order -- the canonical row has to be resolved whether
    # it happens to sort before or after the alias that points at it.
    decisions = []                         # (author_id, name, dynasty, rung, bio, source)
    for author_id, name, dynasty in rows:
        source_bio = bios.get((name, dynasty))
        if source_bio is not None:
            decisions.append((author_id, name, dynasty, "exact",
                              source_bio[0], source_bio[1]))
            continue
        # Rung 2: a hand-written alias says this spelling is that person. It
        # only fires if the canonical spelling resolves on its own, so an alias
        # can never invent a biography the sources do not hold.
        canonical = aliases.get((name, dynasty))
        if canonical is not None and (canonical, dynasty) in bios:
            decisions.append((author_id, name, dynasty, "alias",
                              bios[(canonical, dynasty)][0],
                              bios[(canonical, dynasty)][1]))
            continue
        # Rung 3: the only author row in the database with this name, and
        # exactly one source describes it, under another dynasty. Both
        # conditions matter: the first keeps a name the corpus has already split
        # across dynasties out of it, the second keeps a name two sources
        # disagree about out of it.
        dynasties = bio_dynasties.get(name) or set()
        if len(by_name[name]) == 1 and len(dynasties) == 1 and dynasty not in dynasties:
            other = next(iter(dynasties))
            decisions.append((author_id, name, dynasty, DYNASTY_GAP,
                              bios[(name, other)][0], bios[(name, other)][1]))
            continue
        decisions.append((author_id, name, dynasty, "stub", None, None))

    conn.execute("UPDATE author SET bio=NULL, bio_folded=NULL, bio_short=NULL, "
                 "bio_source=NULL")
    conn.execute("DELETE FROM author_alias")
    conn.execute("DELETE FROM unmatched_author")
    conn.execute("DELETE FROM author_match_stat")

    rungs = collections.Counter()          # rung -> author rows
    rung_works = collections.Counter()     # rung -> works, the weighted number
    unmatched = []
    aliased = []

    for author_id, name, dynasty, rung, bio, source in decisions:
        works = works_by_id.get(author_id, 0)
        rungs[rung] += 1
        rung_works[rung] += works

        if rung == "stub":
            # A stub keeps work.author_id NOT NULL, so "author unknown" is a
            # query -- WHERE bio_source='missing' -- rather than a NULL check
            # at every use site.
            conn.execute("UPDATE author SET bio_source='missing' WHERE id=?",
                         (author_id,))
        else:
            conn.execute(
                "UPDATE author SET bio=?, bio_folded=?, bio_short=?, bio_source=? "
                "WHERE id=?", (bio, folding.segment(bio), first_sentence(bio),
                               source, author_id))

        if rung == "alias":
            # Point the alias row at the canonical author, so the decision is
            # queryable and not merely applied. The canonical row keeps its own
            # works: a folded-to-the-same spelling is a search no-op, and
            # re-pointing works would merge two rows the corpus kept apart.
            canonical = aliases[(name, dynasty)]
            conn.execute(
                "INSERT OR REPLACE INTO author_alias(alias, dynasty, author_id) "
                "VALUES (?, ?, ?)",
                (name, dynasty, ids_by_name[canonical][dynasty]))
            aliased.append((name, dynasty, canonical))

        if rung in ("stub", DYNASTY_GAP):
            # Rung 3 is recorded although it succeeded: it is the one rung that
            # reasons across the boundary the rest of the schema refuses to
            # cross, and a guess that leaves no trace is indistinguishable from
            # a fact.
            dynasties = sorted(bio_dynasties.get(name) or ())
            unmatched.append((
                name, dynasty,
                DYNASTY_GAP if rung == DYNASTY_GAP
                else (AMBIGUOUS if dynasties else NO_BIO),
                works,
                json.dumps([row[0] for row in conn.execute(
                    "SELECT id FROM work WHERE author_id=? ORDER BY id LIMIT 3",
                    (author_id,))]),
            ))

    conn.executemany(
        "INSERT INTO unmatched_author(name, dynasty, cause, work_count, samples) "
        "VALUES (?, ?, ?, ?, ?)", unmatched,
    )
    conn.executemany(
        "INSERT INTO author_match_stat(rung, authors, works) VALUES (?, ?, ?)",
        [(rung, rungs[rung], rung_works[rung]) for rung in rungs],
    )
    conn.commit()

    total_works = sum(works_by_id.values())
    resolved = total_works - rung_works["stub"]
    return {
        "enabled": True,
        "rungs": dict(rungs),
        "works": works_by_id,
        "unmatched": len(unmatched),
        "unmatched_works": total_works - resolved,
        "resolved": 100.0 * resolved / total_works if total_works else 0.0,
        "dropped": dict(dropped),
        "bio_keys": len(bios),
        "aliases": aliased,
        "alias_rows": len(aliases),
    }


def report(db_path, stream=sys.stdout, top=15):
    """Print what the biography pass achieved, read back out of the database.

    Deliberately reads the tables rather than the stats dict: the numbers a
    person checks should be the numbers a query sees, and ``author_match_stat``
    is what a caller who cannot rerun the build has to go on.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        rungs = conn.execute(
            "SELECT rung, authors, works FROM author_match_stat "
            "ORDER BY works DESC").fetchall()
        if not rungs:
            print("no biography pass recorded; run with --full", file=stream)
            return 1
        total_authors = conn.execute("SELECT count(*) FROM author").fetchone()[0]
        total_works = conn.execute("SELECT count(*) FROM work").fetchone()[0]
        print("== biography match, by rung ==", file=stream)
        resolved = 0
        for rung, authors, works in rungs:
            print("  %-12s %6d authors  %7d works  %6.2f%%"
                  % (rung, authors, works,
                     100.0 * works / total_works if total_works else 0.0),
                  file=stream)
            if rung != "stub":
                resolved += works
        print("  %-12s %6d authors  %7d works  %6.2f%%"
              % ("resolved", total_authors - sum(
                  a for r, a, _ in rungs if r == "stub"), resolved,
                 100.0 * resolved / total_works if total_works else 0.0),
              file=stream)

        print("\n== where the unresolved works are ==", file=stream)
        causes = conn.execute(
            "SELECT cause, count(*), coalesce(sum(work_count), 0) "
            "FROM unmatched_author GROUP BY cause ORDER BY 3 DESC").fetchall()
        for cause, keys, works in causes:
            note = ("" if cause == DYNASTY_GAP else
                    "  (recorded, not attached)")
            if cause == DYNASTY_GAP:
                note = "  (attached, across a dynasty boundary)"
            print("  %-12s %5d keys  %7d works%s" % (cause, keys, works, note),
                  file=stream)

        print("\n== coverage by dynasty (the number a regression would move) ==",
              file=stream)
        for dynasty, authors, works, with_bio in conn.execute(
                "SELECT a.dynasty, count(DISTINCT a.id), count(w.id), "
                "       count(DISTINCT CASE WHEN a.bio IS NOT NULL THEN a.id END) "
                "FROM author a LEFT JOIN work w ON w.author_id = a.id "
                "GROUP BY a.dynasty ORDER BY 3 DESC"):
            print("  %-10s %6d authors  %7d works  %6.2f%% biographied"
                  % (dynasty, authors, works,
                     100.0 * with_bio / authors if authors else 0.0),
                  file=stream)

        if top:
            print("\n== biggest unresolved authors ==", file=stream)
            for name, dynasty, cause, works in conn.execute(
                    "SELECT name, dynasty, cause, work_count FROM unmatched_author "
                    "WHERE cause != ? ORDER BY work_count DESC LIMIT ?",
                    (DYNASTY_GAP, top)):
                print("  %-14s %-8s %5d works  %s"
                      % (name, dynasty, works, cause), file=stream)
            print("\n== biographies attached across a dynasty boundary ==",
                  file=stream)
            for name, dynasty, works in conn.execute(
                    "SELECT name, dynasty, work_count FROM unmatched_author "
                    "WHERE cause = ? ORDER BY work_count DESC", (DYNASTY_GAP,)):
                print("  %-14s %-8s %5d works" % (name, dynasty, works),
                      file=stream)
        return 0
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m loader.authors",
        description="Attach biographies to the authors in a built database.")
    parser.add_argument("--db", default=os.path.join(REPO_ROOT, "build", "poetry.db"),
                        help="database to work on")
    parser.add_argument("--config", default=DATAS_CONFIG,
                        help="datas.json holding the biographies block")
    parser.add_argument("--aliases", default=ALIASES_PATH,
                        help="hand-written alias table")
    parser.add_argument("--repo-root", default=REPO_ROOT,
                        help="root the biography paths are relative to")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="re-run the biography pass (the schema must exist)")
    parser.add_argument("--report", action="store_true",
                        help="print the match report and exit")
    args = parser.parse_args(argv)

    if args.do_import:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        conn = sqlite3.connect(args.db)
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            stats = import_biographies(conn, config, repo_root=args.repo_root,
                                       aliases_path=args.aliases)
        finally:
            conn.close()
        if not stats.get("enabled"):
            print("no 'biographies' block in %s" % args.config, file=sys.stderr)
            return 2
        print("biographies: %.2f%% of works resolved (%d keys / %d works unresolved)"
              % (stats["resolved"], stats["unmatched"], stats["unmatched_works"]))

    if args.report or not args.do_import:
        return report(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
