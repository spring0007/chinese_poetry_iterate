# -*- coding: utf-8 -*-
"""Traditional -> Simplified folding and FTS5 tokenisation for the corpus.

Two jobs live here, and they are deliberately separate:

``fold``
    Map a string into Simplified space. The corpus is split down the middle —
    全唐诗 / 全宋诗 / 御定全唐詩 are traditional (99.2% / 100% / 100%) while
    宋词 / 元曲 / 楚辞 / 论语 / 诗经 / 纳兰性德 are simplified. Searching 春风
    against 全唐诗 returns nothing, because the text says 春風. That is a
    correctness defect, not a performance one: measured on 全唐诗, 春风 returns
    0 rows and 春風 returns 7251.

``segment``
    Space a folded string out so that FTS5's ``unicode61`` tokeniser indexes
    each CJK character as its own token. FTS5's ``trigram`` tokeniser was
    measured and rejected: it silently returns zero rows for two-character
    Chinese queries, and two-character words are the common case.

The invariant that everything else leans on is that **folding preserves
character count**. ``len(fold(s)) == len(s)`` means an offset found in the
index maps straight back to the same offset in the displayed original, which
is what lets highlights be computed with ``str.find`` in Python (rather than
FTS5's ``snippet()``, which cannot see the unfolded text) and what lets the
``exact=True`` escape hatch re-filter with ``LIKE`` over the folded column.
``fold_checked`` reports any string that breaks the invariant so the builder
can record it instead of silently corrupting offsets.

One-to-many folding (後/后, 幹/干, 隻/只) does not pollute stored text, because
``line.text`` is written once from the source JSON and ``fold`` is a pure
function applied to a copy. The real cost is recall inflation: 後宮 and 皇后
both fold to strings containing 后, so a one-character query for 后 over-matches.
That is a superset — it never returns a *wrong* answer — and ``exact=True``
removes it. No reverse index of ambiguous folds is kept; the ambiguity is
inherent to the language, not to this code.
"""
import functools

__version__ = "1"
"""Folding behaviour version.

Recorded alongside ``schema_version`` when a database is built. If a later
release changes how folding works and an incremental build runs against a
database built by an older version, the two halves of the index would be in
different spaces; the builder compares this and refuses rather than producing
a silently half-folded index.
"""

#: Sentinels for the character classes ``segment`` distinguishes.
_HAN = "han"
_WORD = "word"
_OTHER = "other"

#: Glyph variants that are *not* traditional/simplified pairs, so no T/S
#: converter can supply them — zhconv returns both forms of each pair
#: unchanged in every direction (zh-cn, zh-hans, zh-tw, zh-hant). They are the
#: same character written with two different Unicode codepoints, which means
#: folding alone leaves them split and a search for either form misses the
#: other.
#:
#: Seeded from the 17-entry table the corpus maintainers hand-maintained in
#: ``宋词/UpdateCi.py``. Of those 17 entries, 16 are real T/S pairs that zhconv
#: reproduces exactly; ``舃``/``舄`` is the one that is not, and is the only
#: entry that has to be carried here. Both forms really do occur: 舃 62 times,
#: 舄 552 times.
#:
#: Every entry must replace one character with one character, so that folding
#: keeps the length invariant described in the module docstring. Add pairs
#: here only with corpus evidence that both forms are in use.
_VARIANT_PAIRS = {
    "舃": "舄",  # U+8203 -> U+8204
}

_VARIANTS = {ord(src): dst for src, dst in _VARIANT_PAIRS.items()}


def _zhconv():
    """Import ``zhconv`` on first use.

    Deliberately lazy: ``loader/data_loader.py`` and everything built on it
    predate this module and keep working with no third-party package
    installed. Only building or querying the unified database needs zhconv.
    """
    try:
        import zhconv  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "loader.folding needs the 'zhconv' package to fold traditional "
            "characters to Simplified. Install it with: pip install zhconv"
        ) from exc
    return zhconv


def _is_han(ch):
    """True for characters that should each become their own FTS token."""
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF        # CJK Extension A
        or 0x4E00 <= code <= 0x9FFF     # CJK Unified Ideographs
        or 0xF900 <= code <= 0xFAFF     # CJK Compatibility Ideographs
        or 0x20000 <= code <= 0x3FFFF   # CJK Extensions B and beyond
        or code == 0x3007               # 〇, the ideographic zero
    )


def _classify(ch):
    if _is_han(ch):
        return _HAN
    if ch.isalnum():
        return _WORD
    return _OTHER


@functools.lru_cache(maxsize=8192)
def _fold_char(ch):
    return _zhconv().convert(ch.translate(_VARIANTS), "zh-cn")


@functools.lru_cache(maxsize=32768)
def fold_checked(text):
    """Fold to Simplified, returning ``(folded, length_preserved)``.

    ``length_preserved`` is False only for the handful of strings whose
    folding changes the character count. Those cannot be re-aligned against
    the original, so the builder records them in ``build/fold_exceptions.json``
    rather than letting them shift every highlight after them.
    """
    if not text:
        return text, True

    folded = _zhconv().convert(text.translate(_VARIANTS), "zh-cn")
    if len(folded) == len(text):
        return folded, True

    # A phrase-table entry changed the character count. Degrade to folding one
    # character at a time: this gives up phrase-level correctness (乾隆 would
    # fold to 干隆) but only for strings that would otherwise misalign every
    # subsequent offset. A phrase whose *folded* form is also a different
    # length from the source is reported as not preserved.
    per_char = "".join(_fold_char(c) for c in text)
    return per_char, len(per_char) == len(text)


@functools.lru_cache(maxsize=32768)
def fold(text):
    """Fold ``text`` to Simplified, preserving character count.

    >>> fold("春風萬里")
    '春风万里'
    >>> fold("明月")
    '明月'
    """
    return fold_checked(text)[0]


def segment(text):
    """Space ``text`` out so each CJK character is its own FTS5 token.

    >>> segment("春風萬里abc")
    '春 風 萬 里 abc'
    >>> segment("明，月")
    '明， 月'

    Consecutive non-CJK alphanumerics stay in one token, so ASCII words are
    still searchable as words. Punctuation and whitespace pass through
    untouched: ``unicode61`` treats them as separators anyway, and removing
    them would erase the difference between text that had a space there and
    text that did not.

    The transformation is deliberately NOT reversible by deleting spaces.
    ``segment`` swallows a run of whitespace that sits next to a Han
    character, so ``REPLACE(segment(t), ' ', '') == t`` only holds while ``t``
    has no space between two Han characters — ``segment("春 风")`` is
    ``"春 风"``, which strips back to ``"春风"``. Nothing may reconstruct the
    original from ``folded``; ``exact=True`` compares against ``line.text``,
    which holds the folded text itself.

    Idempotent: ``segment(segment(s)) == segment(s)``.
    """
    out = []
    # True when the previous emitted character would run into the next one.
    need_space = False
    for ch in text:
        cls = _classify(ch)
        if cls == _OTHER:
            out.append(ch)
            need_space = False
        elif cls == _HAN:
            if out and out[-1] != " ":
                out.append(" ")
            out.append(ch)
            need_space = True
        else:  # _WORD
            if need_space and out:
                out.append(" ")
            out.append(ch)
            need_space = False
    return "".join(out)


def fts_query(user_text):
    """Turn user input into an FTS5 ``MATCH`` phrase.

    >>> fts_query("春風")
    '"春 風"'
    >>> fts_query("春风")
    '"春 風"'

    Traditional and Simplified input produce the *same* string, which is the
    whole point: ``--search 春风`` and ``--search 春風`` must return one set of
    results.

    Everything is wrapped in a single quoted phrase, so FTS5 operators
    (``*``, ``^``, ``NEAR``, ``AND``, ``-``, ``:``) in user input are literal
    text and cannot alter the query — verified against a real FTS5 table with
    ``明月" OR "x``, ``NEAR(a b)`` and ``^^^``, none of which raise or match
    anything unintended. Embedded double quotes are doubled, which is FTS5's
    escape inside a quoted string.

    Because the whole query is one phrase, punctuation in the input acts as a
    separator rather than an operator: ``明，月`` and ``明月`` both become
    ``"明 月"`` and find the same rows. There is deliberately no support for
    ``-``/``AND``/``OR`` — the phrase semantics are the predictable ones, and
    a query that is nothing but punctuation raises rather than silently
    matching no rows.
    """
    tokens = [
        token
        for token in segment(fold(user_text)).split()
        # A token of pure punctuation survives splitting but contributes no
        # token to the index, so FTS5 would match nothing with it.
        if any(_classify(ch) != _OTHER for ch in token)
    ]
    if not tokens:
        raise ValueError(
            "query has no indexable characters: %r" % (user_text,)
        )
    return '"%s"' % " ".join(tokens).replace('"', '""')
