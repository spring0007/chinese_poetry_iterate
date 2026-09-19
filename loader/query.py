# -*- coding: utf-8 -*-
"""Search and browse ``build/poetry.db``.

    python -m loader.query --search 春风 --dynasty tang
    python -m loader.query --search 春風                  # the same rows
    python -m loader.query --search 明月 --exact          # literal, not merely near
    python -m loader.query --author 王建                  # every 王建 in the corpus
    python -m loader.query --work 321573

Three things a caller has to know before leaning on this module.

**Everything is Simplified.** The database stores folded text, so a query is
folded before it reaches the index and ``春风`` and ``春風`` are one query.
The folding lives in ``loader.folding``; none of it is repeated here. Stored
text comes back as it is stored — Simplified — and the traditional original is
one ``source_file.relative_path``, ``work.ordinal`` and ``line.idx`` away in
the corpus JSON.

**A phrase is a phrase, and ``exact=True`` is the literal.** FTS5 matches the
characters of a query even when punctuation stands between them, because
``unicode61`` discards punctuation when tokenising: ``明月`` finds
``夜气清而明，月照空山``. Over a corpus of regulated verse that is usually
what a reader wants, since the punctuation is editorial. ``exact=True`` adds a
substring test and drops those rows. The test reads ``line.text`` — the
displayed text — and not the segmented shadow column, because ``segment`` is
not reversible by deleting spaces: a test against the folded column would
report a literal match for text that has a space where the query has none.

**Identical poems appear once.** 全唐诗 and 御定全唐詩 are two editions of one
anthology, so 6,503 groups of works here have byte-identical bodies and a
search for one poem would otherwise return it two to five times.
``search`` collapses such a group into its first member and lists the rest
under ``Hit.editions``. That is a grouping, not a deletion — nothing is hidden,
``group=False`` shows every copy, and ``--all`` on the command line does the
same.

The connection this module opens sets ``PRAGMA query_only``, so nothing here
can damage a build even if it tried.
"""
import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, replace

from . import folding

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(REPO_ROOT, "build", "poetry.db")

#: Rows per ``IN (...)`` batch. Comfortably under SQLite's default
#: SQLITE_MAX_VARIABLE_NUMBER (999 on older builds, 32766 on 3.32+) so the
#: same code runs anywhere, and large enough that per-query overhead vanishes.
#: A three-character query over the whole corpus matches 158,051 works, which
#: is 176 batches — the batching is not decorative.
_CHUNK = 900

#: Separator inside a body key. A control character, because no line of poetry
#: contains one, which is what makes the key unambiguous rather than merely
#: usually right.
_BODY_SEP = "\x1f"

#: A grouped search has to read the bodies of more works than the page it will
#: return, since one page of twenty could be four poems in five editions each.
#: The window is bounded by ``max(_MIN_SCAN, (offset + limit) * _SCAN_FACTOR)``
#: works. It is a heuristic, which is why ``count()`` — which is exact and
#: cheap — exists and why the command line reports its totals from there.
_MIN_SCAN = 2500
_SCAN_FACTOR = 50


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Line:
    """One line of a work, Simplified, as stored."""

    id: int
    idx: int
    text: str
    char_len: int


@dataclass(frozen=True)
class Work:
    """A poem, song, article or chapter, with the labels it is catalogued under."""

    id: int
    title: str
    author_name: str         # the spelling this dataset publishes
    dynasty: str
    kind: str
    rhythmic: str
    line_count: int
    ordinal: int
    external_id: str
    author_id: int
    author: str              # author.name, the canonical spelling
    dataset: str             # dataset.key
    path: str                # source_file.relative_path


@dataclass(frozen=True)
class Hit:
    """One work in a result set, with the lines that matched in it.

    ``editions`` holds the other works whose body is identical to this one —
    the same poem as another dataset or anthology prints it. They are the
    members of a group that ``search`` collapsed; with ``group=False`` each
    would be a ``Hit`` of its own and this tuple would be empty.
    """

    work: Work
    lines: tuple
    editions: tuple = ()

    @property
    def also_titles(self):
        """``(dataset, title)`` for every collapsed edition, for display."""
        return tuple((other.dataset, other.title) for other in self.editions)


@dataclass(frozen=True)
class Author:
    id: int
    name: str
    dynasty: str
    bio: str
    bio_short: str
    bio_source: str
    work_count: int


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def connect(db_path=None):
    """Open the database read-only.

    ``PRAGMA query_only`` rather than ``file:...?mode=ro``: a read-only URI
    cannot open a database that is in WAL mode with an uncheckpointed log,
    which is exactly the state a build leaves it in, and it would fail at the
    one moment a reader is most likely to be running. ``query_only`` is a
    connection-level guard that works either way.
    """
    path = os.path.abspath(db_path or DEFAULT_DB)
    if not os.path.exists(path):
        raise FileNotFoundError(
            "no database at %s\nbuild one with:\n"
            "    python -m loader.build_db --db %s --full" % (path, path)
        )
    conn = sqlite3.connect(path)
    # Autocommit: a reader has no use for an implicit transaction, and leaving
    # one open would hold a lock against the next build.
    conn.isolation_level = None
    conn.execute("PRAGMA query_only=ON")
    return conn


# --------------------------------------------------------------------------
# the SQL every query is built from
# --------------------------------------------------------------------------
# Written once. ``_work_of`` reads the columns of ``_WORK_COLUMNS`` in exactly
# this order and ``_line_of`` reads ``_LINE_COLUMNS`` likewise, so the two
# lists and the two unpackers have to be edited together.

_LINE_COLUMNS = "line.id, line.idx, line.text, line.char_len"

_WORK_COLUMNS = (
    "work.id, work.title, work.author_name, work.dynasty, work.kind, "
    "work.rhythmic, work.line_count, work.ordinal, work.external_id, "
    "author.id, author.name, dataset.key, source_file.relative_path"
)

_AUTHOR_COLUMNS = (
    "author.id, author.name, author.dynasty, author.bio, author.bio_short, "
    "author.bio_source, author.work_count"
)

_WORK_JOINS = """
    JOIN author      ON author.id  = work.author_id
    JOIN dataset     ON dataset.id = work.dataset_id
    JOIN source_file ON source_file.id = work.source_file_id
"""

# These begin with the newline that separates them from what precedes them, so
# that a caller appending one to a column list cannot fuse the last column name
# into the FROM keyword. "…relative_path" + "FROM …" is a syntax error SQLite
# reports at a token nowhere near the mistake.
_FROM = ("\nFROM line_fts JOIN line ON line.id = line_fts.rowid"
         "\n    JOIN work ON work.id = line.work_id" + _WORK_JOINS)

_WORK_FROM = "\nFROM work" + _WORK_JOINS

#: How many columns ``_LINE_COLUMNS`` contributes, so that a hit row can be
#: split into (line, work) without counting commas at the call site.
_LINE_WIDTH = 4


def _line_of(row, at=0):
    return Line(id=row[at], idx=row[at + 1], text=row[at + 2], char_len=row[at + 3])


def _work_of(row, at):
    return Work(
        id=row[at], title=row[at + 1], author_name=row[at + 2], dynasty=row[at + 3],
        kind=row[at + 4], rhythmic=row[at + 5], line_count=row[at + 6],
        ordinal=row[at + 7], external_id=row[at + 8], author_id=row[at + 9],
        author=row[at + 10], dataset=row[at + 11], path=row[at + 12],
    )


def _author_of(row):
    return Author(id=row[0], name=row[1], dynasty=row[2], bio=row[3],
                  bio_short=row[4], bio_source=row[5], work_count=row[6])


def _chunked(conn, ids, sql_for, params=()):
    """Run a query over ``ids`` in batches, concatenating the rows.

    ``sql_for(placeholders)`` returns the SQL with the ``IN (...)`` clause as
    its **last** condition, and ``params`` are the values that come before it.
    Both halves of that convention are load-bearing: without it the batch
    values and the query's own values would interleave in the wrong order.
    """
    rows = []
    ordered = sorted(ids)
    for start in range(0, len(ordered), _CHUNK):
        batch = ordered[start:start + _CHUNK]
        sql = sql_for(",".join("?" * len(batch)))
        rows.extend(conn.execute(sql, tuple(params) + tuple(batch)).fetchall())
    return rows


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

def exact_needle(text):
    """``text`` as the ``LIKE`` pattern ``exact=True`` looks for.

    Folded, because the stored text is folded and a query for 春風 has to
    become 春风 before it can be compared with anything. Escaped, because a
    reader may search for ``%`` or ``_`` and would otherwise be handed a
    wildcard that matches text they did not ask for. The backslash goes first;
    escaping ``%`` before it would double the backslashes it had just added.
    """
    folded = folding.fold(text)
    for ch in ("\\", "%", "_"):
        folded = folded.replace(ch, "\\" + ch)
    return "%" + folded + "%"


def _filters(dynasty=None, dataset=None, author=None):
    """The optional WHERE fragments, and the values that go with them."""
    clauses, values = [], []
    if dynasty:
        clauses.append("work.dynasty = ?")
        values.append(dynasty)
    if dataset:
        clauses.append("dataset.key = ?")
        values.append(dataset)
    if author:
        # The canonical spelling, so one person's works are one result even
        # where the datasets spell the name differently.
        clauses.append("author.name = ?")
        values.append(folding.fold(author))
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), values


def _where(fts, needle):
    if needle is None:
        return "line_fts MATCH ?", [fts]
    return "line_fts MATCH ? AND line.text LIKE ? ESCAPE '\\'", [fts, needle]


def _terms(text, exact):
    """``(fts phrase, LIKE needle or None)`` for one user query.

    The single place a user string turns into SQL conditions, so that a count
    and the search it counts can never disagree about what was asked for.
    """
    return folding.fts_query(text), (exact_needle(text) if exact else None)


# --------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------

def body_keys(conn, work_ids):
    """Map each work id to a key that is equal exactly when its body is.

    The body is the sequence of folded lines in ``idx`` order, joined with a
    character no poem contains. Two works get the same key if and only if they
    are the same text line for line, which is what makes "the same poem in
    another anthology" a comparison rather than a guess.

    Ordered in SQL by ``(work_id, idx)`` and joined in Python rather than with
    ``group_concat``, whose row order is unspecified and would make the key
    depend on the query plan.

    A work with no lines at all is absent from the mapping, which keeps empty
    works from being reported as editions of one another.
    """
    parts = {}
    rows = _chunked(
        conn, work_ids,
        lambda places: "SELECT work_id, folded FROM line WHERE work_id IN (%s) "
                       "ORDER BY work_id, idx" % places,
    )
    for work_id, folded in rows:
        parts.setdefault(work_id, []).append(folded)
    return {work_id: _BODY_SEP.join(lines) for work_id, lines in parts.items()}


def editions(conn, work_id):
    """The other works that carry exactly this body.

    Candidates are the works whose *first* line is this work's first line.
    Two works with identical bodies must agree on their first line, so no
    edition can be missed, and the search for them is an index lookup instead
    of a scan of 389,343 works. The candidates are then confirmed against the
    whole body, which is what rejects a poem that merely opens the same way.
    """
    opening = conn.execute(
        "SELECT text FROM line WHERE work_id = ? ORDER BY idx LIMIT 1", (work_id,)
    ).fetchone()
    mine = body_keys(conn, [work_id]).get(work_id)
    if opening is None or mine is None:
        return []
    try:
        fts = folding.fts_query(opening[0])
    except ValueError:
        # A first line with no indexable characters — punctuation only. There
        # is no phrase to look up, and no honest way to find its twins.
        return []

    others = [row[0] for row in conn.execute(
        "SELECT DISTINCT line.work_id " + _FROM +
        " WHERE line_fts MATCH ? AND line.idx = 0 AND line.work_id <> ?",
        (fts, work_id)).fetchall()]
    if not others:
        return []
    theirs = body_keys(conn, others)
    return [_work(conn, other) for other in sorted(others)
            if theirs.get(other) == mine]


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def _scan(conn, text, exact, filters, scan):
    """``(WHERE, values, work ids)`` for a query.

    Returns the WHERE clause and its values as well as the ids, because a
    caller that goes on to fetch the matching *lines* has to ask the same
    question again — and asking it a second time by rebuilding the clause is
    how a count and the search it counts come to disagree.
    """
    fts, needle = _terms(text, exact)
    where, params = _where(fts, needle)
    extra, values = _filters(**filters)
    where, params = where + extra, params + values
    sql = ("SELECT DISTINCT line.work_id " + _FROM + " WHERE " + where +
           " ORDER BY line.work_id")
    id_params = params
    if scan is not None:
        sql += " LIMIT ?"
        id_params = params + [scan]
    ids = [row[0] for row in conn.execute(sql, id_params).fetchall()]
    return where, params, ids


def count(conn, text, exact=False, **filters):
    """``(lines, works)`` matching ``text``. Exact, and cheap: no grouping.

    Split out from ``search`` because a grouped result cannot say how much it
    is a page *of* — it only knows the window it read.
    """
    fts, needle = _terms(text, exact)
    where, params = _where(fts, needle)
    extra, values = _filters(**filters)
    row = conn.execute(
        "SELECT count(*), count(DISTINCT line.work_id) " + _FROM +
        " WHERE " + where + extra, tuple(params) + tuple(values)).fetchone()
    return row[0], row[1]


def grouped_count(conn, text, exact=False, **filters):
    """How many distinct bodies the match set holds. Exact, and unbounded.

    The number ``search`` shows a page of when it is grouping, and the reason
    it can be stated at all: bodies are cheap next to lines, so this reads the
    6,503 duplicate groups without reading a single line of text.
    """
    _, _, work_ids = _scan(conn, text, exact, filters, None)
    if not work_ids:
        return 0
    return len(set(body_keys(conn, work_ids).values()))


def matching_line_ids(conn, text, exact=False, **filters):
    """Every ``line.id`` the query matches, as a set.

    The contract ``exact=True`` is pinned against: a phrase query, then a
    literal substring test on the display text. Kept public because a caller
    comparing two spellings of a query — 春风 and 春風 — has no other way to
    ask whether they really are one query.
    """
    fts, needle = _terms(text, exact)
    where, params = _where(fts, needle)
    extra, values = _filters(**filters)
    rows = conn.execute("SELECT line.id " + _FROM + " WHERE " + where + extra,
                        tuple(params) + tuple(values)).fetchall()
    return {row[0] for row in rows}


def search(conn, text, exact=False, dynasty=None, dataset=None, author=None,
           group=True, limit=None, offset=0, scan=None):
    """The works containing ``text``, one ``Hit`` per work, in work order.

    A hit carries the lines of that work which matched, so a poem whose second
    and fifth lines both contain the query is one result and not two. With
    ``group`` (the default) works whose whole body is identical are collapsed
    into the first of them, which is what stops 全唐诗 and 御定全唐詩 from
    answering every search twice; the others are listed on ``Hit.editions``.

    ``limit=None`` reads the whole match set, so the result is complete and the
    grouping is exact. A limited result is a page of a windowed scan — see the
    note on ``_MIN_SCAN`` — and ``editions`` on a hit at the edge of the window
    may be short. Use ``count()`` for totals.
    """
    if scan is None and limit is not None:
        if not group:
            # Without grouping a hit is a work, so a page needs exactly the
            # works it will show and no window is needed.
            scan = offset + limit
        else:
            scan = max(_MIN_SCAN, (offset + limit) * _SCAN_FACTOR)

    where, params, work_ids = _scan(
        conn, text, exact,
        {"dynasty": dynasty, "dataset": dataset, "author": author}, scan)
    if not work_ids:
        return []

    rows = _chunked(
        conn, work_ids,
        lambda places: "SELECT " + _LINE_COLUMNS + ", " + _WORK_COLUMNS +
                       _FROM + " WHERE " + where + " AND line.work_id IN (%s) "
                       "ORDER BY line.work_id, line.idx" % places,
        params,
    )
    hits = _bucket(rows)
    if group:
        hits = _collapse(conn, hits)
    return hits[offset:] if limit is None else hits[offset:offset + limit]


def _bucket(rows):
    """One ``Hit`` per work, its matching lines in ``idx`` order.

    The rows arrive ordered by work, and ``_chunked`` keeps that order because
    it walks a sorted id list in contiguous batches, so the works can be
    collected with a dict and a list of first-seen keys.
    """
    order, works, lines = [], {}, {}
    for row in rows:
        work = _work_of(row, _LINE_WIDTH)
        if work.id not in works:
            order.append(work.id)
            works[work.id] = work
            lines[work.id] = []
        lines[work.id].append(_line_of(row))
    return [Hit(work=works[work_id], lines=tuple(lines[work_id]))
            for work_id in order]


def _collapse(conn, hits):
    """Fold works with identical bodies into one hit each, keeping the first.

    ``Hit`` is frozen, so a survivor that gains an edition is replaced rather
    than mutated. Works with no body key are passed through untouched.
    """
    keys = body_keys(conn, [hit.work.id for hit in hits])
    first_at, out = {}, []
    for hit in hits:
        key = keys.get(hit.work.id)
        at = None if key is None else first_at.get(key)
        if at is None:
            if key is not None:
                first_at[key] = len(out)
            out.append(hit)
        else:
            out[at] = replace(out[at], editions=out[at].editions + (hit.work,))
    return out


# --------------------------------------------------------------------------
# single records
# --------------------------------------------------------------------------

def _work(conn, work_id):
    row = conn.execute("SELECT " + _WORK_COLUMNS + _WORK_FROM +
                       " WHERE work.id = ?", (work_id,)).fetchone()
    return _work_of(row, 0) if row else None


def work(conn, work_id):
    """One work, all of its lines, and the editions it also appears in.

    ``None`` if there is no such work. The editions cost a scan of the work's
    twins and are worth a separate call for a caller walking many works, but
    the interesting question about a single work is usually "why do I keep
    seeing this one twice".
    """
    item = _work(conn, work_id)
    if item is None:
        return None
    lines = tuple(_line_of(row) for row in conn.execute(
        "SELECT " + _LINE_COLUMNS + " FROM line WHERE work_id = ? ORDER BY idx",
        (work_id,)).fetchall())
    return Hit(work=item, lines=lines, editions=tuple(editions(conn, work_id)))


def author(conn, name, dynasty=None):
    """Every author row answering to ``name``, most prolific first.

    A list, not a row, and that is the point: 王建 is a Tang poet and a Song
    poet, 李白 is one person, and the schema keys authors on
    ``(name, dynasty)`` precisely so that the first pair cannot be merged. A
    lookup that returned one row would have to choose.

    The name is matched against ``author.name``, which is where the biography
    sources put the canonical spelling. Only if that finds nothing does it fall
    back to ``work.author_name``, the spelling the datasets publish — a work
    whose author never got a biography still has an author, and the stub row
    the builder created for it is keyed on the spelling the works use.
    """
    folded = folding.fold(name)
    sql = "SELECT " + _AUTHOR_COLUMNS + " FROM author WHERE author.name = ?"
    params = [folded]
    if dynasty:
        sql += " AND author.dynasty = ?"
        params.append(dynasty)
    sql += " ORDER BY author.work_count DESC, author.id"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        sql = ("SELECT DISTINCT " + _AUTHOR_COLUMNS +
               " FROM author JOIN work ON work.author_id = author.id "
               "WHERE work.author_name = ?")
        params = [folded]
        if dynasty:
            sql += " AND author.dynasty = ?"
            params.append(dynasty)
        sql += " ORDER BY author.work_count DESC, author.id"
        rows = conn.execute(sql, params).fetchall()
    return [_author_of(row) for row in rows]


def author_works(conn, author_id, limit=None, offset=0):
    """Every work attributed to ``author_id``, in the order it was built."""
    sql = ("SELECT " + _WORK_COLUMNS + _WORK_FROM +
           " WHERE work.author_id = ? ORDER BY work.id")
    params = [author_id]
    if limit is not None or offset:
        # ``LIMIT -1`` is SQLite's "no limit": an offset is meaningful without
        # a limit, and quietly ignoring it would be a worse answer than the
        # one SQLite requires this spelling for.
        sql += " LIMIT ? OFFSET ?"
        params += [-1 if limit is None else limit, offset]
    return [_work_of(row, 0) for row in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _title(title):
    return title if title else "(无题)"


def _headline(hit, position):
    work = hit.work
    where = "%s:%d" % (work.path, work.ordinal)
    line = "%3d. %s  %s" % (position, _title(work.title), work.author)
    meta = "  ".join(part for part in (work.dynasty, work.kind, work.rhythmic,
                                       work.dataset) if part)
    return "%s\n     %s\n     %s" % (line, meta, where)


def _print_hits(hits):
    for position, hit in enumerate(hits, 1):
        print(_headline(hit, position))
        for line in hit.lines:
            print("       %s" % line.text)
        if hit.editions:
            others = ", ".join("%s《%s》" % (work.dataset, _title(work.title))
                               for work in hit.editions)
            print("     + %d 个相同正文的版本: %s" % (len(hit.editions), others))


def _print_search(conn, args):
    kwargs = {"exact": args.exact, "dynasty": args.dynasty,
              "dataset": args.dataset}
    if args.author:
        kwargs["author"] = args.author

    lines, works = count(conn, args.search, **kwargs)
    total = works
    scope = []
    if args.dataset:
        scope.append(args.dataset)
    if args.dynasty:
        scope.append(args.dynasty)
    if args.author:
        scope.append(args.author)
    if args.exact:
        scope.append("exact")
    print("%s  %s  →  %d 行 / %d 首%s"
          % (args.search, args.dynasty or "全部", lines, works,
             "  (%s)" % ", ".join(scope) if scope else ""))
    if args.count:
        if not args.all:
            print("  合并相同正文后 %d 篇" % grouped_count(conn, args.search, **kwargs))
        return

    hits = search(conn, args.search, group=not args.all, limit=args.limit,
                  offset=args.offset, **kwargs)
    if not hits:
        print("  (没有匹配)")
        return
    last = args.offset + len(hits)
    if args.all:
        print("  显示 %d-%d / %d 首（每一份复制单独列出）" % (args.offset + 1, last, works))
    else:
        groups = grouped_count(conn, args.search, **kwargs)
        print("  显示 %d-%d / %d 篇（%d 首相同正文已合并，--all 展开）"
              % (args.offset + 1, last, groups, works - groups))
        total = groups
    _print_hits(hits)
    if args.limit is not None and last < total:
        print("  ... 还有 %d 篇，用 --offset %d 继续" % (total - last, last))


def _print_work(conn, args):
    hit = work(conn, args.work)
    if hit is None:
        print("没有 id 为 %d 的作品" % args.work, file=sys.stderr)
        return 1
    print(_headline(hit, args.work))
    for line in hit.lines:
        print("       %s" % line.text)
    if hit.editions:
        print("     + %d 个相同正文的版本:" % len(hit.editions))
        for other in hit.editions:
            print("       %s《%s》 %s:%d"
                  % (other.dataset, _title(other.title), other.path, other.ordinal))
    return 0


def _print_authors(conn, args, works=False):
    found = author(conn, args.author, dynasty=args.dynasty)
    if not found:
        print("没有名为 %s 的作者" % args.author)
        return
    print("%s  →  %d 位" % (args.author, len(found)))
    for person in found:
        bio = person.bio_short or ("(简介待补)" if person.bio is None else "")
        print("  #%d  %s  %s  作品 %d  %s"
              % (person.id, person.name, person.dynasty, person.work_count, bio))
        if works:
            for item in author_works(conn, person.id, limit=args.limit):
                print("       %s  %s" % (_title(item.title), item.path))
    if not works and len(found) == 1:
        print("  用 --author-id %d 列出这位作者的作品" % found[0].id)


def _print_author_works(conn, args):
    rows = author_works(conn, args.author_id, limit=args.limit, offset=args.offset)
    if not rows:
        print("作者 #%d 没有作品，或者不存在" % args.author_id)
        return
    print("#%d 的 %d 首作品（显示 %d-%d）"
          % (args.author_id, len(rows), args.offset + 1,
             args.offset + len(rows)))
    for item in rows:
        print("  %-14s %s  %s:%d"
              % (_title(item.title), item.dynasty, item.path, item.ordinal))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="loader.query", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB, help="the database to read")
    parser.add_argument("--search", metavar="TEXT",
                        help="find works containing TEXT (Simplified or traditional)")
    parser.add_argument("--work", type=int, metavar="ID", help="show one work")
    parser.add_argument("--author", metavar="NAME",
                        help="with --search, restrict to this author; alone, look the name up")
    parser.add_argument("--author-id", type=int, metavar="N",
                        help="list one author's works")
    parser.add_argument("--exact", action="store_true",
                        help="only literal matches, not ones a clause break splits")
    parser.add_argument("--dynasty", metavar="KEY", help="tang / song / yuan / ...")
    parser.add_argument("--dataset", metavar="KEY", help="e.g. tangsong, songci")
    parser.add_argument("--all", action="store_true",
                        help="show every copy of a duplicated body, not one")
    parser.add_argument("--count", action="store_true", help="totals only")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)

    # A Windows console still defaults to a legacy code page, and a single
    # glyph outside it would otherwise abort the query it was only a detail of.
    # Replace the character, keep the result.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    try:
        conn = connect(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        if args.search:
            _print_search(conn, args)
        elif args.work is not None:
            return _print_work(conn, args) or 0
        elif args.author_id is not None:
            _print_author_works(conn, args)
        elif args.author:
            _print_authors(conn, args)
        else:
            parser.error("one of --search, --work, --author, --author-id is required")
    except ValueError as exc:
        # folding.fts_query refuses a query with nothing indexable in it.
        print(exc, file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
