# -*- coding: utf-8 -*-
"""Build ``build/poetry.db`` from the corpus.

    python -m loader.build_db --db build/poetry.db --full
    python -m loader.build_db --db build/poetry.db --full --only songci

The builder reads ``loader/datas.json``, walks each dataset's files the same
way ``PlainDataLoader`` does, turns every record into a ``loader.mapping.Work``
and writes it into the schema in ``loader/schema.sql``. It never writes to the
corpus: the JSON is input only.

Everything it stores is Simplified. ``work.title``, ``line.text``,
``author.name`` and the rest are all folded on the way in, so the database
speaks one script and a search cannot miss half the corpus for spelling
reasons. The original is not lost — it is still in the JSON, and
``source_file.relative_path`` plus ``work.ordinal`` and ``line.idx`` point at
it exactly.

Transaction granularity is one source file. A file is the natural unit: it is
what changes, it is what the ledger records, and a failure leaves the database
consistent at a file boundary with the failed file still dirty for next time.

A *full* build takes the FTS triggers down for the duration and rebuilds the
index once at the end. The triggers are right for incremental work, where a
handful of rows change and the index has to follow them; over 1.7M rows of a
fresh build they are pure overhead, and the cost of maintaining the index
incrementally grows as the index does. Measured on 全唐诗: 83.6 s to write
1,369,180 lines with the triggers down plus 5.2 s for one 'rebuild', against
779.5 s with the triggers up. The two databases answer every test query
identically, line for line.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from . import folding, mapping

# Imported by name, not as ``authors``: inside build() that name is the
# AuthorBook instance tallying rows, and the module would be shadowed by it.
# ``report`` likewise has to be aliased -- this module has one of its own.
from .authors import import_biographies, report as authors_report

#: Bumped when schema.sql changes in a way an existing database cannot absorb.
SCHEMA_VERSION = "1"
GENERATOR = "loader.build_db/1"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
DATAS_CONFIG = os.path.join(HERE, "datas.json")
ALIASES_PATH = os.path.join(HERE, "author_aliases.json")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path):
    """Open a connection with the pragmas this schema needs.

    ``PRAGMA foreign_keys`` is per-connection and defaults to OFF — it is not
    a property of the database file, so it has to be issued on *every*
    connection that writes. Without it the ON DELETE CASCADE from work to line
    never fires, and the FTS index silently keeps rows for text that is gone.

    WAL rather than the default rollback journal so the database stays
    readable while a build runs.

    ``isolation_level = None`` turns off the sqlite3 module's implicit
    transaction handling. Without it the module opens a transaction of its own
    before the first INSERT, and the explicit ``BEGIN IMMEDIATE`` that gives
    each file its transaction boundary would then fail with "cannot start a
    transaction within a transaction".
    """
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def create_schema(conn):
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        conn.executescript(handle.read())


def schema_trigger_names():
    """The triggers schema.sql creates, read from the file itself.

    Taken from the source rather than written down twice: a list maintained by
    hand next to the file it describes drifts, and the drift would show up as
    a missing index rather than as a failure.
    """
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        return sorted(re.findall(r"CREATE TRIGGER\s+(\w+)", handle.read(), re.I))


def drop_fts_triggers(conn):
    """Take every FTS trigger down; return the DDL needed to put them back.

    Only ever safe when the whole database is being written and the index will
    therefore be rebuilt from scratch at the end. With the triggers down the
    DELETE path no longer maintains the index, so the stale rows a rewrite
    would otherwise leave behind survive — harmlessly, because 'rebuild'
    discards the entire index before repopulating it from the content table.
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ).fetchall()
    for name, _ in rows:
        conn.execute("DROP TRIGGER %s" % name)
    return [(name, sql) for name, sql in rows]


def restore_fts_triggers(conn, ddl):
    """Put back exactly what ``drop_fts_triggers`` removed."""
    for _, sql in ddl:
        conn.execute(sql)


def rebuild_fts(conn):
    """Rebuild all three indexes from their content tables, in one pass each."""
    for table in ("line_fts", "work_fts", "author_fts"):
        conn.execute("INSERT INTO %s(%s) VALUES ('rebuild')" % (table, table))
    conn.commit()


def check_triggers(conn, db_path):
    """Refuse to write a database whose triggers are not all present.

    A process killed between the drop and the restore leaves the schema
    without them. Everything then still runs and still returns results; the
    index just stops following the content table, which is the one failure
    this schema is built to make impossible. So it is checked, not assumed.
    """
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    missing = [name for name in schema_trigger_names() if name not in present]
    if missing:
        raise SystemExit(
            "%s is missing %d of its FTS trigger(s): %s\nThe index would stop "
            "tracking the content table. Re-run with --full."
            % (db_path, len(missing), ", ".join(missing))
        )


def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config(path=DATAS_CONFIG):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class AuthorBook:
    """Maps ``(name, dynasty)`` to an author row, creating one on first sight.

    v1 creates every author here, as a stub with no biography; phase 4 imports
    the five biography files and fills those rows in, matching on exactly this
    key. Creating them from the works first means ``work.author_id`` is never
    NULL and "who has no biography" is one query rather than a NULL check at
    every call site.

    The ``(name, dynasty)`` pair and not the name alone: 490 names appear in
    more than one biography file, and 王建 in 全唐诗 and 王建 in 宋词 are
    different people. Merging them would be silent and unrecoverable.

    ``bio_source`` is left NULL rather than 'missing' — at this stage every
    author looks the same, and writing 'missing' would be a claim about phase
    4's outcome that phase 2 cannot know.
    """

    def __init__(self, conn):
        self._conn = conn
        self._cache = {}
        self.created = 0

    def id_for(self, name, dynasty):
        key = (name, dynasty)
        known = self._cache.get(key)
        if known is not None:
            return known
        row = self._conn.execute(
            "SELECT id FROM author WHERE name = ? AND dynasty = ?", key
        ).fetchone()
        if row is None:
            cursor = self._conn.execute(
                "INSERT INTO author(name, name_folded, dynasty) VALUES (?, ?, ?)",
                (name, folding.segment(name), dynasty),
            )
            author_id = cursor.lastrowid
            self.created += 1
        else:
            author_id = row[0]
        self._cache[key] = author_id
        return author_id


def register_datasets(conn, config):
    """One ``dataset`` row per entry in datas.json, before anything is read."""
    conn.execute("BEGIN IMMEDIATE")
    for key, dataset in config["datasets"].items():
        conn.execute(
            "INSERT INTO dataset(id, key, name, path, tag, dynasty, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                dataset["id"],
                key,
                dataset["name"],
                dataset["path"],
                dataset["tag"],
                mapping.dynasty_for(dataset, ""),  # "" matches a "*" rule only
                dataset.get("kind"),
            ),
        )
    conn.commit()


def build_file(conn, dataset_id, key, dataset, path, authors, exceptions,
               repo_root=REPO_ROOT):
    """Parse, map and write one source file. Returns ``(works, lines)``.

    The caller has already opened a transaction; this function does no
    committing of its own so that the file is all-or-nothing.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    relative = mapping.relative_path(path, repo_root)
    stat = os.stat(path)
    cursor = conn.execute(
        "INSERT INTO source_file(dataset_id, relative_path, size_bytes, "
        "mtime_ns, sha1, parsed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (dataset_id, relative, stat.st_size, stat.st_mtime_ns, sha1_of(path), utcnow()),
    )
    source_file_id = cursor.lastrowid

    works = lines = 0
    line_rows = []
    for work in mapping.iter_works(
        key, dataset, path, data, repo_root, exceptions=exceptions
    ):
        author_id = authors.id_for(work.author, work.dynasty)
        cursor = conn.execute(
            "INSERT INTO work(dataset_id, source_file_id, ordinal, external_id, "
            "title, title_folded, author_id, author_name, dynasty, kind, "
            "rhythmic, rhythmic_folded, tags, line_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                source_file_id,
                work.ordinal,
                work.external_id,
                work.title,
                folding.segment(work.title) if work.title else None,
                author_id,
                work.author,
                work.dynasty,
                work.kind,
                work.rhythmic,
                folding.segment(work.rhythmic) if work.rhythmic else None,
                json.dumps(work.tags, ensure_ascii=False) if work.tags else None,
                len(work.lines),
            ),
        )
        work_id = cursor.lastrowid
        works += 1
        for idx, text in enumerate(work.lines):
            # folded is the *segmented* form, and it must equal exactly what
            # the FTS index holds — see the note at the top of schema.sql.
            line_rows.append(
                (work_id, idx, text, folding.segment(text), len(text))
            )
            lines += 1
        if len(line_rows) >= 10000:
            conn.executemany(
                "INSERT INTO line(work_id, idx, text, folded, char_len) "
                "VALUES (?, ?, ?, ?, ?)",
                line_rows,
            )
            line_rows = []
    if line_rows:
        conn.executemany(
            "INSERT INTO line(work_id, idx, text, folded, char_len) "
            "VALUES (?, ?, ?, ?, ?)",
            line_rows,
        )

    conn.execute(
        "UPDATE source_file SET work_count = ?, line_count = ? WHERE id = ?",
        (works, lines, source_file_id),
    )
    return works, lines


def recompute_counts(conn):
    """Refresh the denormalised counters. Cheap once, and only at the end."""
    conn.execute(
        "UPDATE author SET work_count = "
        "(SELECT count(*) FROM work WHERE work.author_id = author.id)"
    )
    conn.execute(
        "UPDATE dataset SET "
        "work_count = (SELECT count(*) FROM work WHERE work.dataset_id = dataset.id), "
        "line_count = (SELECT count(*) FROM line JOIN work ON work.id = line.work_id "
        "              WHERE work.dataset_id = dataset.id)"
    )


def write_meta(conn, stats):
    for key, value in (
        ("schema_version", SCHEMA_VERSION),
        ("folding_version", folding.__version__),
        ("built_at", utcnow()),
        ("generator", GENERATOR),
    ):
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def build(db_path, full=False, only=None, quiet=False, exceptions_path=None,
          repo_root=None, config_path=None, aliases_path=None):
    """Build or rebuild ``db_path``. Returns a stats dict.

    ``repo_root`` and ``config_path`` exist so tests can build a synthetic
    corpus in a temporary directory. They default to this repository, which is
    the only thing the command line ever uses.
    """
    repo_root = os.path.abspath(repo_root or REPO_ROOT)
    config = load_config(config_path or DATAS_CONFIG)
    datasets = config["datasets"]
    if only:
        unknown = [key for key in only if key not in datasets]
        if unknown:
            raise SystemExit(
                "unknown dataset(s): %s\nknown: %s"
                % (", ".join(unknown), ", ".join(sorted(datasets)))
            )

    if full and os.path.exists(db_path):
        # WAL leaves -wal and -shm siblings; removing the database alone would
        # let a stale WAL be replayed into the new file.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    fresh = not os.path.exists(db_path)
    conn = connect(db_path)
    dropped = None
    try:
        if fresh:
            create_schema(conn)
        else:
            check_triggers(conn, db_path)
            existing = dict(conn.execute("SELECT key, value FROM schema_meta"))
            if existing.get("folding_version") != folding.__version__:
                raise SystemExit(
                    "database was built with folding version %r, this code has "
                    "%r. The two halves of the index would be in different "
                    "character spaces. Re-run with --full."
                    % (existing.get("folding_version"), folding.__version__)
                )
            if existing.get("schema_version") != SCHEMA_VERSION:
                raise SystemExit(
                    "database schema is %r, this code expects %r. Re-run with "
                    "--full." % (existing.get("schema_version"), SCHEMA_VERSION)
                )
            if not only:
                raise SystemExit(
                    "incremental builds arrive in phase 5, and this is not one: "
                    "without --full or --only the build would find every file "
                    "already in the ledger and silently do nothing. Re-run with "
                    "--full."
                )

        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT INTO build_run(started_at, mode) VALUES (?, ?)",
            (utcnow(), "full" if full or fresh else "partial"),
        )
        run_id = cursor.lastrowid
        conn.commit()

        if fresh or full:
            register_datasets(conn, config)
            # This build owns every row in the database, so the index can be
            # built once at the end instead of maintained row by row. See the
            # note at the top of this module for the measurement behind it.
            conn.execute("BEGIN IMMEDIATE")
            dropped = drop_fts_triggers(conn)
            conn.commit()

        authors = AuthorBook(conn)
        exceptions = []
        totals = {"files": 0, "works": 0, "lines": 0}
        clock = time.time()

        for key, dataset in datasets.items():
            if only and key not in only:
                continue
            mapping.validate_spec(key, dataset)
            # Drop this dataset's previous rows before rewriting them. On a
            # partial build the cascade source_file -> work -> line fires
            # line_ad, so the index is corrected by the trigger rather than by
            # any code here. On a full build the triggers are down and the
            # rows this leaves behind in the index are discarded wholesale by
            # the 'rebuild' at the end.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM source_file WHERE dataset_id = ?", (dataset["id"],)
            )
            conn.commit()
            files = mapping.dataset_files(dataset, repo_root)
            dataset_works = dataset_lines = 0
            for i, path in enumerate(files, 1):
                # One transaction per file: a failure leaves the database at a
                # file boundary and that file still dirty.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    works, lines = build_file(
                        conn, dataset["id"], key, dataset, path, authors, exceptions,
                        repo_root=repo_root,
                    )
                except Exception:
                    conn.rollback()
                    raise
                conn.commit()
                dataset_works += works
                dataset_lines += lines
                totals["files"] += 1
                totals["works"] += works
                totals["lines"] += lines
                if not quiet and (i % 50 == 0 or i == len(files)):
                    sys.stderr.write(
                        "\r  %-20s %4d/%-4d files  %7d works  %8d lines  %5.1fs"
                        % (key, i, len(files), dataset_works, dataset_lines,
                           time.time() - clock)
                    )
                    sys.stderr.flush()
            if not quiet and files:
                sys.stderr.write("\n")

        # Biographies are a post-pass over the authors the loop above created,
        # not a second loader: the join key is folded on both sides, so which
        # author rows exist cannot depend on whether the biographies went in
        # first. Running it here -- after every author exists, before the FTS
        # rebuild -- also means author_fts indexes bio_folded in the same pass
        # as everything else, instead of through 12,500 trigger firings.
        conn.execute("BEGIN IMMEDIATE")
        try:
            author_stats = import_biographies(
                conn, config, repo_root=repo_root,
                aliases_path=aliases_path or ALIASES_PATH,
            )
        except Exception:
            conn.rollback()
            raise
        conn.commit()

        if dropped is not None:
            # Restore before rebuilding, so the schema is never left
            # trigger-less for longer than the build itself.
            conn.execute("BEGIN IMMEDIATE")
            restore_fts_triggers(conn, dropped)
            conn.commit()
            dropped = None
            rebuild_fts(conn)

        conn.execute("BEGIN IMMEDIATE")
        recompute_counts(conn)
        write_meta(conn, totals)
        conn.execute(
            "UPDATE build_run SET finished_at=?, files_seen=?, files_parsed=?, "
            "works_written=?, lines_written=?, fold_exceptions=? WHERE id=?",
            (utcnow(), totals["files"], totals["files"], totals["works"],
             totals["lines"], len(exceptions), run_id),
        )
        conn.commit()

        # 'optimize' merges the index's b-tree segments; without it a freshly
        # built index is correct but slow to query. It is significant work, so
        # it runs once at the end rather than per file.
        if totals["files"]:
            conn.execute("INSERT INTO line_fts(line_fts) VALUES ('optimize')")
            conn.execute("INSERT INTO work_fts(work_fts) VALUES ('optimize')")
            conn.execute("INSERT INTO author_fts(author_fts) VALUES ('optimize')")
        conn.execute("PRAGMA optimize")

        if exceptions_path:
            parent = os.path.dirname(os.path.abspath(exceptions_path))
            os.makedirs(parent, exist_ok=True)
            with open(exceptions_path, "w", encoding="utf-8") as handle:
                json.dump(exceptions, handle, ensure_ascii=False, indent=1)

        totals["authors"] = authors.created
        totals["author_stats"] = author_stats
        totals["fold_exceptions"] = exceptions
        totals["seconds"] = time.time() - clock
        totals["db_bytes"] = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        return totals
    finally:
        # Reached with ``dropped`` set only when the build failed, since the
        # success path clears it. Repairing the schema matters more than the
        # exception that got us here, and this must not raise over the top of
        # it — check_triggers will catch an unrepaired database on the next run.
        if dropped is not None:
            try:
                conn.rollback()
                restore_fts_triggers(conn, dropped)
                conn.commit()
            except sqlite3.Error as exc:
                sys.stderr.write("could not restore FTS triggers: %s\n" % exc)
        conn.close()


def report(conn_or_path, totals):
    print("built %s in %.1fs" % (totals.get("db_path", "?"), totals["seconds"]))
    print("  files %d   works %d   lines %d   new authors %d"
          % (totals["files"], totals["works"], totals["lines"], totals["authors"]))
    if totals["fold_exceptions"]:
        print("  fold changed the length of %d string(s); recorded, not silently used"
              % len(totals["fold_exceptions"]))
    stats = totals.get("author_stats") or {}
    if stats.get("enabled"):
        print("  biographies: %.2f%% of works have one (%d keys / %d works without)"
              % (stats["resolved"], stats["unmatched"], stats["unmatched_works"]))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="loader.build_db", description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.path.join(REPO_ROOT, "build", "poetry.db"))
    parser.add_argument("--full", action="store_true",
                        help="discard any existing database and rebuild everything")
    parser.add_argument("--only", action="append", metavar="KEY",
                        help="build only this dataset (repeatable); still needs --full on a fresh db")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--exceptions", metavar="PATH",
                        default=os.path.join(REPO_ROOT, "build", "fold_exceptions.json"))
    parser.add_argument("--aliases", metavar="PATH",
                        default=os.path.join(REPO_ROOT, "loader", "author_aliases.json"),
                        help="hand-written author alias table")
    parser.add_argument("--report", action="store_true",
                        help="print the full biography match report after building")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db) and not args.full:
        print("no database at %s; building from scratch" % args.db, file=sys.stderr)
    totals = build(args.db, full=args.full, only=args.only, quiet=args.quiet,
                   exceptions_path=args.exceptions, aliases_path=args.aliases)
    totals["db_path"] = args.db
    report(args.db, totals)
    if args.report:
        print()
        authors_report(args.db)


if __name__ == "__main__":
    main()
