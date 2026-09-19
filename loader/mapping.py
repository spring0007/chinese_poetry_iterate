# -*- coding: utf-8 -*-
"""Turn each dataset's own JSON shape into one common ``Work`` object.

The corpus is not one format. ``loader/datas.json`` already declares where the
body of a record lives (``tag``: ``paragraphs`` here, ``content`` there,
``para`` somewhere else), but it says nothing about where the *title* is, or
the author, or the 全唐诗 UUID — so nothing downstream could ever ask "find
every poem by 王建" without a hard-coded per-dataset branch.

This module adds that declaration, and it is deliberately declarative rather
than a set of Python adapters. Eleven of the fourteen datasets are a plain
array of records whose fields differ only in *where* they sit; adding a
fifteenth should mean editing JSON, not writing code. That is what "easy to
keep updated" has to mean for a repository that follows an upstream corpus.

The path grammar has five rules, and no more:

    ``$``            the record itself
    ``.name``        a key of a dict
    ``[*]``          every element of a list
    ``[0]``          one element of a list
    ``|``            ordered alternatives; the first that resolves at all wins

So ``$.title`` is a title, ``$.paragraphs[*]`` is every paragraph,
``$.content|$.paragraphs`` is "whichever of these two keys this record uses",
and ``$.a[*].b`` fans out. Anything the grammar cannot express belongs in a
named adapter, not in a more clever grammar: the 蒙学 files nest three levels
deep with a middle key that means *genre* in one file and *volume* in another,
and no amount of path syntax can know which is which. Inventing a conditional
mini-language to cover that would be harder to test and harder to debug than
the dozen lines of Python it replaces.

Where the declared shape and the file disagree, this module fails loudly. That
is the point of declaring the shape at all: the ``songci`` body key was wrong
in ``datas.json`` for as long as it took someone to read the file, and a
validator that reads the declaration back would have caught it on day one.
"""
import fnmatch
import os
from dataclasses import dataclass, field

__version__ = "1"

#: Top-level shapes a dataset file may have. ``dict_or_list`` exists because
#: 四书五经/ genuinely disagrees with itself: ``daxue.json`` and
#: ``zhongyong.json`` are bare ``{chapter, paragraphs}`` dicts while
#: ``mengzi.json`` is an array of records with that same shape.
ROOTS = ("list", "dict", "dict_or_list")

#: Dynasty values are the corpus' own: ``元曲/yuanqu.json`` carries a per-record
#: ``dynasty`` field and every one of its 11,057 records says ``"yuan"``. Using
#: the corpus' convention rather than Chinese names keeps a join against it
#: possible and matches the ``--dynasty tang`` the CLI was specified with.
DYNASTIES = ("xianqin", "han", "wudai", "tang", "song", "yuan", "qing")

#: Attributed when neither the record nor the dataset names an author. The
#: classical convention for "author not recorded"; 论语, 诗经 and 四书五经 have
#: no author field at all, and their works would otherwise all collapse onto a
#: single unnamed stub.
DEFAULT_AUTHOR = "佚名"


class PathError(ValueError):
    """A path expression is malformed."""


class SpecError(ValueError):
    """A dataset's mapping declaration is wrong, or the file contradicts it."""


# --------------------------------------------------------------------------
# The path grammar
# --------------------------------------------------------------------------

def _parse_alternative(text):
    """One ``...`` alternative, as a list of ``(kind, argument)`` steps."""
    steps = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == ".":
            j = i + 1
            while j < n and text[j] not in ".[":
                j += 1
            key = text[i + 1:j]
            if not key:
                raise PathError("empty key in %r" % (text,))
            steps.append(("key", key))
            i = j
        elif ch == "[":
            j = text.find("]", i)
            if j < 0:
                raise PathError("unclosed '[' in %r" % (text,))
            inner = text[i + 1:j]
            if inner == "*":
                steps.append(("each", None))
            elif inner.isdigit():
                steps.append(("index", int(inner)))
            else:
                raise PathError(
                    "expected '[*]' or '[n]' in %r, got %r" % (text, inner)
                )
            i = j + 1
        else:
            raise PathError(
                "unexpected %r at offset %d of %r" % (ch, i, text)
            )
    return steps


def parse_path(expr):
    """Compile an expression into a list of alternatives (each a step list).

    >>> parse_path("$.paragraphs")
    [[('key', 'paragraphs')]]
    >>> parse_path("$.a[*].b|$.c")
    [[('key', 'a'), ('each', None), ('key', 'b')], [('key', 'c')]]
    """
    if not isinstance(expr, str) or not expr:
        raise PathError("path must be a non-empty string, got %r" % (expr,))
    alternatives = []
    for part in expr.split("|"):
        part = part.strip()
        if not part.startswith("$"):
            raise PathError(
                "每一条路径都必须以 '$' 开头: %r in %r" % (part, expr)
            )
        alternatives.append(_parse_alternative(part[1:]))
    return alternatives


def leaf_keys(expr):
    """The dict keys an expression can end on — used to check ``tag``.

    >>> leaf_keys("$.content|$.paragraphs")
    ['content', 'paragraphs']
    """
    out = []
    for steps in parse_path(expr):
        for kind, argument in reversed(steps):
            if kind == "key":
                out.append(argument)
                break
        else:
            out.append(None)  # ends on [*] or $, so it names no key
    return out


def walk(node, expr):
    """Every value ``expr`` selects from ``node``; empty if it selects none.

    Never raises for a missing key — a missing key is a legitimate outcome
    that lets a later ``|`` alternative be tried. Callers that *require* a
    value use :func:`first`.
    """
    for steps in parse_path(expr):
        values = [node]
        for kind, argument in steps:
            nxt = []
            for value in values:
                if kind == "key":
                    if isinstance(value, dict) and argument in value:
                        nxt.append(value[argument])
                elif kind == "each":
                    if isinstance(value, list):
                        nxt.extend(value)
                else:
                    if isinstance(value, list) and argument < len(value):
                        nxt.append(value[argument])
            values = nxt
            if not values:
                break
        if values:
            return values
    return []


def first(node, expr, default=None):
    """The first value ``expr`` selects, or ``default``."""
    values = walk(node, expr)
    return values[0] if values else default


# --------------------------------------------------------------------------
# What a record becomes
# --------------------------------------------------------------------------

@dataclass
class Work:
    """One poem, song, article or chapter, whatever dataset it came from.

    Every text field is Simplified by the time a ``Work`` leaves this module;
    ``build_db`` writes it straight into ``work``/``line`` without folding
    again. Folding here rather than there keeps the one rule — everything that
    reaches the database is folded — in a single place.
    """

    dataset_key: str
    source_file: str            # forward slashes, relative to the repository root
    ordinal: int                # index of the record within its file
    title: str = None
    author: str = None
    dynasty: str = None
    kind: str = None
    rhythmic: str = None
    external_id: str = None
    tags: list = field(default_factory=list)
    lines: list = field(default_factory=list)

    @property
    def char_count(self):
        return sum(len(line) for line in self.lines)


def normalise_lines(value):
    """Flatten whatever the body path selected into a list of strings.

    The body is a ``list`` in twelve datasets and a single ``str`` in
    ``幽梦影/youmengying.json``, which is why this cannot simply be a list
    comprehension over the selected value. Nested lists are flattened too,
    which is what the 蒙学 files will need in phase 2.

    (``PlainDataLoader.body_extractor`` does ``body += poem[tag]`` and so
    explodes that one string into 219 lists of single characters. That is a
    pre-existing quirk of an API that must keep behaving identically; it is
    not repeated here.)
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(normalise_lines(item))
        return out
    if isinstance(value, (int, float)):
        return [str(value)]
    raise SpecError(
        "body path selected a %s where text was expected: %.60r"
        % (type(value).__name__, value)
    )


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------

def effective_mapping(dataset):
    """The mapping block for a dataset, synthesising the obvious default.

    A dataset that declares only ``tag`` — which is every entry written before
    this module existed — still builds: the body is ``$.<tag>``, the title is
    ``$.title``, and so on. That keeps onboarding a new dataset to one field
    while leaving full control available when its shape is not the usual one.
    """
    mapping = dict(dataset.get("mapping") or {})
    tag = dataset.get("tag")
    mapping.setdefault("root", dataset.get("root", "list"))
    mapping.setdefault("author", "$.author")
    mapping.setdefault("title", "$.title")
    mapping.setdefault("dynasty", "$.dynasty")
    if tag is not None:
        mapping.setdefault("body", "$." + tag)
    mapping.setdefault("rhythmic", "$.rhythmic")
    mapping.setdefault("external_id", "$.id")
    mapping.setdefault("tags", "$.tags")
    return mapping


def validate_spec(key, dataset):
    """Raise :class:`SpecError` if a dataset's declaration is unusable.

    Checks the declaration against itself only — that every path compiles, the
    root is one of the three known shapes, and that the declared ``tag`` is
    actually one of the keys the body path can land on. The last one is the
    check that would have caught ``songci``'s ``authors.song.json``.
    """
    if "tag" not in dataset:
        raise SpecError("%s: no 'tag'" % key)
    for required in ("name", "id", "path"):
        if required not in dataset:
            raise SpecError("%s: no %r" % (key, required))

    root = dataset.get("root", "list")
    if root not in ROOTS:
        raise SpecError(
            "%s: root must be one of %s, got %r" % (key, list(ROOTS), root)
        )

    mapping = effective_mapping(dataset)
    for field_name, expr in sorted(mapping.items()):
        if field_name in ("root",):
            continue
        if not isinstance(expr, str):
            raise SpecError(
                "%s: mapping.%s must be a string, got %r"
                % (key, field_name, expr)
            )
        parse_path(expr)  # raises PathError, which is a ValueError

    body_keys = leaf_keys(mapping["body"])
    if dataset["tag"] not in body_keys:
        raise SpecError(
            "%s: tag is %r but mapping.body %r can land on %s"
            % (key, dataset["tag"], mapping["body"], body_keys)
        )

    for rule in dataset.get("dynasty_rules") or []:
        if "match" not in rule or "dynasty" not in rule:
            raise SpecError(
                "%s: every dynasty_rules entry needs 'match' and 'dynasty', "
                "got %r" % (key, rule)
            )


def dynasty_for(dataset, filename):
    """The dynasty for a record, from the file it came from.

    全唐诗/ is one dataset holding two dynasties: the files are
    ``poet.tang.NNNN.json`` and ``poet.song.NNNN.json``, so a dataset-level
    default plus glob rules is all that is needed. The first matching rule
    wins, and the dataset default is the fallback — so the conventional last
    rule is ``{"match": "*", "dynasty": ...}``.
    """
    for rule in dataset.get("dynasty_rules") or []:
        if fnmatch.fnmatch(filename, rule["match"]):
            return rule["dynasty"]
    return dataset.get("dynasty")


def record_root(data, key, declared):
    """Unwrap a parsed file into the list of records it holds.

    ``PlainDataLoader._as_records`` accepts all three shapes unconditionally,
    because it predates the declaration and could not change behaviour. Here
    the declaration is available and is honoured: a dataset that says ``list``
    but holds a bare dict has a wrong declaration, and saying so is more
    useful than quietly doing something the declaration does not describe.
    """
    if isinstance(data, list):
        if declared == "dict":
            raise SpecError("%s: declared root 'dict', file holds a list" % key)
        return data
    if isinstance(data, dict):
        if declared == "list":
            raise SpecError(
                "%s: declared root 'list', file holds a bare dict — "
                "use root: 'dict' or 'dict_or_list'" % key
            )
        return [data]
    raise SpecError(
        "%s: file root is %s, expected an object or array"
        % (key, type(data).__name__)
    )


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def dataset_files(dataset, repo_root):
    """Every JSON file a dataset contributes, as absolute paths, sorted.

    Mirrors ``PlainDataLoader.body_extractor``'s traversal — a single file, or
    a directory minus ``excludes`` — with one difference: non-``.json`` files
    are skipped rather than opened. ``excludes`` already names every such file
    in the corpus, so the two agree today; ``test_mapping.py`` asserts that,
    because a divergence would mean the old loader and the new builder read
    different data.
    """
    full = os.path.join(repo_root, dataset["path"])
    if os.path.isfile(full):
        return [full]
    excludes = set(dataset.get("excludes") or [])
    out = []
    for name in sorted(os.listdir(full)):
        if name in excludes or not name.endswith(".json"):
            continue
        path = os.path.join(full, name)
        if os.path.isfile(path):
            out.append(path)
    return out


def relative_path(path, repo_root):
    """A stable, platform-independent name for a file, for the ledger."""
    return os.path.relpath(path, repo_root).replace(os.sep, "/")


def _fold_field(value, exceptions):
    """Fold one text field, reporting any string that changes length.

    ``fold`` is normally applied directly; this exists so the builder can see
    the handful of strings where ``fold_checked`` reports the length invariant
    broken and record them instead of letting them pass silently.
    """
    from . import folding

    if not value:
        return value, True
    folded, preserved = folding.fold_checked(value)
    if not preserved and exceptions is not None:
        exceptions.append(value)
    return folded, preserved


def iter_works(key, dataset, path, data, repo_root, exceptions=None):
    """Yield a :class:`Work` for every record in one parsed dataset file.

    ``data`` is the already-parsed JSON, passed in rather than read here so
    that the caller decides how much I/O to do — the builder hashes and stats
    the same bytes and should not read the file twice.

    ``exceptions``, if given, collects every string whose folding changed its
    character count. The builder writes them to ``build/fold_exceptions.json``;
    the count is expected to be single digits.
    """
    from . import folding  # local: keeps this module importable without zhconv

    def fold(value):
        return _fold_field(value, exceptions)[0]

    mapping = effective_mapping(dataset)
    declared = mapping["root"]
    relative = relative_path(path, repo_root)
    file_dynasty = dynasty_for(dataset, os.path.basename(path))
    fallback_author = dataset.get("default_author") or DEFAULT_AUTHOR

    for ordinal, record in enumerate(record_root(data, key, declared)):
        # ``walk`` returns the *values* a path selects, so ``$.tags`` yields
        # one value — the array — not its elements. Unwrap that one level;
        # a scalar or a missing key both mean "no tags".
        raw_tags = first(record, mapping["tags"])
        if raw_tags is None:
            tags = []
        elif isinstance(raw_tags, list):
            tags = raw_tags
        else:
            tags = [raw_tags]

        work = Work(
            dataset_key=key,
            source_file=relative,
            ordinal=ordinal,
            title=first(record, mapping["title"]),
            author=first(record, mapping["author"]) or fallback_author,
            dynasty=first(record, mapping["dynasty"], default=file_dynasty),
            kind=dataset.get("kind"),
            rhythmic=first(record, mapping["rhythmic"]),
            external_id=first(record, mapping["external_id"]),
            tags=tags,
            lines=normalise_lines(first(record, mapping["body"])),
        )
        work.title = fold(work.title) if work.title else None
        work.author = fold(work.author) if work.author else None
        work.rhythmic = fold(work.rhythmic) if work.rhythmic else None
        if work.external_id is not None:
            work.external_id = str(work.external_id)
        work.tags = [fold(t) for t in work.tags if isinstance(t, str)]
        work.lines = [fold(line) for line in work.lines]
        yield work
