-- Unified poetry database.
--
-- One SQLite file holding every dataset declared in loader/datas.json, in one
-- shape, with one full-text index over all of it.
--
-- SIMPLIFIED ONLY. Every text column holds folded Simplified characters as
-- produced by loader.folding.fold(). The traditional original is *not* stored.
-- That is a deliberate, reversible choice: the corpus JSON is never rewritten,
-- so the original text is still on disk and every row is traceable back to it
-- through (source_file.relative_path, work.ordinal, line.idx). Folding is a
-- pure function, so the original can always be recovered by re-reading the
-- source; what is given up is reading it straight out of the database.
--
-- The indexed column of each FTS5 table is a *segmented* shadow column: the
-- same text with a space between every CJK character (loader.folding.segment).
-- This is load-bearing, not cosmetic. An external-content FTS5 table stores no
-- copy of its own text; it re-reads the content table when it needs to delete
-- a row. If the content table held unsegmented text while the index held
-- segmented text, the delete would look for a document that does not exist and
-- the FTS row would be silently left behind — and `integrity-check` would still
-- pass. Storing the segmented form means the two agree by construction. The
-- cost is one byte per CJK character (~20 MB corpus-wide) to remove a class of
-- corruption that no standard check can detect.
--
-- Requires SQLite compiled with FTS5 (3.9+). Built and tested on 3.50.4.
--
-- PRAGMA foreign_keys is NOT persistent: it is per-connection and defaults to
-- OFF, so every connection that writes must issue it. build_db.py does.

-- --------------------------------------------------------------------------
-- Bookkeeping
-- --------------------------------------------------------------------------

CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Keys written by the builder:
--   schema_version   this file's version
--   folding_version  loader.folding.__version__
--   built_at         ISO-8601 UTC of the last build
--   generator        e.g. "loader.build_db/1"
-- An incremental build refuses to run when folding_version differs from the
-- installed one: the two halves of the index would be in different character
-- spaces and nothing downstream could tell.

CREATE TABLE dataset (
    id         INTEGER PRIMARY KEY,      -- datas.json "id", preserved as-is
    key        TEXT NOT NULL UNIQUE,     -- datas.json key
    name       TEXT NOT NULL,            -- human-readable, e.g. 全唐诗全宋诗
    path       TEXT NOT NULL,            -- relative to the repository root
    tag        TEXT NOT NULL,            -- the body key, e.g. paragraphs
    dynasty    TEXT,                     -- dataset default
    kind       TEXT,                     -- shi / ci / qu / fu / wen ...
    work_count INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE source_file (
    id            INTEGER PRIMARY KEY,
    dataset_id    INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL UNIQUE,  -- forward slashes, relative to repo root
    size_bytes    INTEGER NOT NULL,      -- change detection, rung 1
    mtime_ns      INTEGER NOT NULL,      -- change detection, rung 1
    sha1          TEXT NOT NULL,         -- change detection, rung 2
    parsed_at     TEXT NOT NULL,         -- ISO-8601 UTC
    work_count    INTEGER NOT NULL DEFAULT 0,
    line_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX source_file_dataset ON source_file(dataset_id);

CREATE TABLE build_run (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    mode          TEXT NOT NULL,         -- full | incremental
    files_seen    INTEGER NOT NULL DEFAULT 0,
    files_parsed  INTEGER NOT NULL DEFAULT 0,
    files_skipped INTEGER NOT NULL DEFAULT 0,
    files_deleted INTEGER NOT NULL DEFAULT 0,
    works_written INTEGER NOT NULL DEFAULT 0,
    lines_written INTEGER NOT NULL DEFAULT 0,
    fold_exceptions INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);

-- --------------------------------------------------------------------------
-- Authors
-- --------------------------------------------------------------------------

CREATE TABLE author (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,           -- Simplified; the source work spelling
    name_folded TEXT NOT NULL,           -- segment(name): the indexed shadow
    dynasty     TEXT NOT NULL,
    bio         TEXT,                    -- Simplified; NULL for a stub
    bio_folded  TEXT,                    -- segment(bio): the indexed shadow
    bio_short   TEXT,                    -- first sentence of bio
    bio_source  TEXT,                    -- which biography file supplied it
    work_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name, dynasty)
);
-- The unique key is (name, dynasty) and NOT (name), and that is the single
-- most important correctness decision in this schema. 490 names occur in more
-- than one biography file: 王建 is in authors.tang.json, authors.song.json and
-- 宋词/author.song.json. 229 tang∩song and 228 song∩宋词 collisions are
-- almost certainly different people who happen to share a name. Keying on
-- name alone would silently merge hundreds of distinct historical figures into
-- one, and nothing downstream would look wrong.
--
-- 'name' is already folded, so a traditional and a simplified spelling of the
-- same name land on the same row without an alias table. That is not a
-- coincidence of this corpus: 趙令畤/赵令畤, 蒲壽宬/蒲寿宬 and two more measured
-- variants all collapse under folding.
--
-- Two things 'name' is NOT. It is not the biography file's spelling: the row is
-- created from the work that mentioned the author, and the biography pass in
-- loader/authors.py fills in 'bio' without ever rewriting 'name'. And it is not
-- re-folded anywhere: fold() is not idempotent on this corpus (朱慶餘 folds to
-- 朱庆馀, but 朱庆馀 folds on to 朱庆余; a phrase-table entry keeps 魏徵 intact
-- while a lone 徵 becomes 征), so folding a stored name again moves the key and
-- loses matches a single fold would have found.

CREATE TABLE author_alias (
    alias   TEXT NOT NULL,               -- Simplified
    dynasty TEXT NOT NULL,
    author_id INTEGER NOT NULL REFERENCES author(id) ON DELETE CASCADE,
    PRIMARY KEY (alias, dynasty)
);
-- Hand-maintained, seeded from loader/author_aliases.json. The rung-2 fallback
-- for names folding cannot join: 魏征/魏徵, 李嘉佑/李嘉祐 and 朱庆余/朱庆馀 are
-- three measured pairs where fold() leaves both spellings standing, so no
-- amount of folding brings them together. Deliberately small -- an automatic
-- one-character-difference rule was measured and rejected, since within a
-- single dynasty it pairs 程垓 with 程颢, 程颐 and forty other 程X.

-- --------------------------------------------------------------------------
-- Works
-- --------------------------------------------------------------------------

CREATE TABLE work (
    id             INTEGER PRIMARY KEY,
    dataset_id     INTEGER NOT NULL REFERENCES dataset(id)    ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,     -- record index within the file
    external_id    TEXT,                 -- 全唐诗 UUID; NULLable on purpose
    title          TEXT,                 -- Simplified
    title_folded   TEXT,                 -- segment(title): the indexed shadow
    author_id      INTEGER NOT NULL REFERENCES author(id),
    author_name    TEXT,                 -- Simplified spelling, as published here
    dynasty        TEXT,
    kind           TEXT,
    rhythmic       TEXT,                 -- 词牌/曲牌, Simplified
    rhythmic_folded TEXT,                -- segment(rhythmic)
    tags           TEXT,                 -- JSON array, or NULL
    line_count     INTEGER NOT NULL DEFAULT 0,
    variant_of     INTEGER REFERENCES work(id),
    UNIQUE(source_file_id, ordinal)
);
-- external_id is NULLable: 唐诗补录.json really does contain one record with
-- "id": null, and 全唐诗/error/ is deferred to phase 2, so no reader may assume
-- it is present or unique.
--
-- variant_of points at the normal-dataset twin of a 全唐诗/error/ record (307
-- text variants). Nothing writes it in v1; the column exists so phase 2 does
-- not need a migration.
--
-- author_id is NOT NULL by design. Every unresolved author gets a stub row
-- (bio IS NULL, bio_source = 'missing'), so "author unknown" is a query
-- (WHERE bio IS NULL AND work_count > 0) rather than a NULL check at every
-- use site.

CREATE INDEX work_dataset    ON work(dataset_id);
CREATE INDEX work_source     ON work(source_file_id);
CREATE INDEX work_author     ON work(author_id);
CREATE INDEX work_dynasty    ON work(dynasty);
CREATE INDEX work_external   ON work(external_id);

CREATE TABLE line (
    id       INTEGER PRIMARY KEY,
    work_id  INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    text     TEXT NOT NULL,              -- Simplified, display form
    folded   TEXT NOT NULL,              -- segment(text): the ONLY indexed column
    char_len INTEGER NOT NULL,
    UNIQUE(work_id, idx)
);
-- 'text' holds Simplified, not the original. Upstream JSON is untouched, so
-- the traditional original still exists on disk and is reachable through
-- source_file.relative_path -> work.ordinal -> line.idx.

CREATE INDEX line_work ON line(work_id);

-- --------------------------------------------------------------------------
-- Cross-references deferred to phase 2
-- --------------------------------------------------------------------------

CREATE TABLE strain (
    external_id TEXT PRIMARY KEY,
    strains     TEXT NOT NULL,           -- JSON array
    source_file TEXT
);
-- 8997 of the ~9000 平仄 rows live in 全唐诗/error/, which v1 does not load.
-- The join key is work.external_id (the 全唐诗 UUID). The table is created
-- empty now so phase 2 adds rows rather than a migration.

-- --------------------------------------------------------------------------
-- Author-match reporting
-- --------------------------------------------------------------------------

CREATE TABLE unmatched_author (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,            -- Simplified, as it could not be resolved
    dynasty    TEXT,
    cause      TEXT NOT NULL,            -- no_bio / ambiguous / dynasty_gap
    work_count INTEGER NOT NULL,
    samples    TEXT                      -- JSON array, at most 3 work ids
);

CREATE TABLE author_match_stat (
    rung       TEXT PRIMARY KEY,         -- exact / alias / dynasty_gap / stub
    authors    INTEGER NOT NULL,
    works      INTEGER NOT NULL          -- weighted: the number that matters
);

-- --------------------------------------------------------------------------
-- Full-text search
-- --------------------------------------------------------------------------
-- Three external-content FTS5 tables, one per searchable level. All use
-- unicode61 rather than trigram: trigram was measured and rejected because it
-- returns zero rows for two-character Chinese queries, and two-character words
-- are the common case in this corpus.
--
-- 'remove_diacritics 0' is the default but is written out deliberately: it
-- disables a folding step that has nothing to do with Chinese and would only
-- be a way for the tokeniser to surprise a future reader.
--
-- Each pair of (table, shadow column) below must stay in step with its trigger
-- trio. Deleting a work cascades to its lines, and CASCADE fires the child's
-- AFTER DELETE triggers (measured), so the index stays correct without any
-- hand-written maintenance in the builder.

CREATE VIRTUAL TABLE line_fts USING fts5(
    folded,
    content='line',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 0"
);

CREATE TRIGGER line_ai AFTER INSERT ON line BEGIN
    INSERT INTO line_fts(rowid, folded) VALUES (new.id, new.folded);
END;

CREATE TRIGGER line_ad AFTER DELETE ON line BEGIN
    INSERT INTO line_fts(line_fts, rowid, folded)
        VALUES ('delete', old.id, old.folded);
END;

CREATE TRIGGER line_au AFTER UPDATE ON line BEGIN
    INSERT INTO line_fts(line_fts, rowid, folded)
        VALUES ('delete', old.id, old.folded);
    INSERT INTO line_fts(rowid, folded) VALUES (new.id, new.folded);
END;

CREATE VIRTUAL TABLE work_fts USING fts5(
    title_folded,
    rhythmic_folded,
    content='work',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 0"
);

CREATE TRIGGER work_ai AFTER INSERT ON work BEGIN
    INSERT INTO work_fts(rowid, title_folded, rhythmic_folded)
        VALUES (new.id, new.title_folded, new.rhythmic_folded);
END;

CREATE TRIGGER work_ad AFTER DELETE ON work BEGIN
    INSERT INTO work_fts(work_fts, rowid, title_folded, rhythmic_folded)
        VALUES ('delete', old.id, old.title_folded, old.rhythmic_folded);
END;

CREATE TRIGGER work_au AFTER UPDATE ON work BEGIN
    INSERT INTO work_fts(work_fts, rowid, title_folded, rhythmic_folded)
        VALUES ('delete', old.id, old.title_folded, old.rhythmic_folded);
    INSERT INTO work_fts(rowid, title_folded, rhythmic_folded)
        VALUES (new.id, new.title_folded, new.rhythmic_folded);
END;

CREATE VIRTUAL TABLE author_fts USING fts5(
    name_folded,
    bio_folded,
    content='author',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 0"
);

CREATE TRIGGER author_ai AFTER INSERT ON author BEGIN
    INSERT INTO author_fts(rowid, name_folded, bio_folded)
        VALUES (new.id, new.name_folded, new.bio_folded);
END;

CREATE TRIGGER author_ad AFTER DELETE ON author BEGIN
    INSERT INTO author_fts(author_fts, rowid, name_folded, bio_folded)
        VALUES ('delete', old.id, old.name_folded, old.bio_folded);
END;

CREATE TRIGGER author_au AFTER UPDATE ON author BEGIN
    INSERT INTO author_fts(author_fts, rowid, name_folded, bio_folded)
        VALUES ('delete', old.id, old.name_folded, old.bio_folded);
    INSERT INTO author_fts(rowid, name_folded, bio_folded)
        VALUES (new.id, new.name_folded, new.bio_folded);
END;
-- bio_folded is populated by loader/authors.py in phase 4; it is NULL until
-- then, and an external-content FTS5 table stores NULL columns fine.
