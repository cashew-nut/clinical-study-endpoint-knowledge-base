"""Text normalisation steps named by matching.yaml's `normalisation` list. The
step order is read from `vocab.matching_normalisation`.

`casefold_unless_case_sensitive` is a no-op here: text stays case-preserved and
each compiled regex carries its own case sensitivity (see conform/matcher.py),
which is the only way the short-acronym rule can be honoured on one prepared
string.
"""

from __future__ import annotations

import re

import duckdb

_DASHES = str.maketrans(
    {c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
)
_QUOTES = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    }
)
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([<>\[\]()*_`~\\])")
_WHITESPACE_RE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text)


def _trim(text: str) -> str:
    return text.strip()


def _normalise_unicode_dashes(text: str) -> str:
    return text.translate(_DASHES)


def _normalise_unicode_quotes(text: str) -> str:
    return text.translate(_QUOTES)


def _unescape_markdown(text: str) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\1", text)


def _casefold_unless_case_sensitive(text: str) -> str:
    return text


_STEPS = {
    "collapse_whitespace": _collapse_whitespace,
    "trim": _trim,
    "normalise_unicode_dashes": _normalise_unicode_dashes,
    "normalise_unicode_quotes": _normalise_unicode_quotes,
    "unescape_markdown": _unescape_markdown,
    "casefold_unless_case_sensitive": _casefold_unless_case_sensitive,
}


def load_normalisation_steps(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            "SELECT step FROM vocab.matching_normalisation ORDER BY ordinal"
        ).fetchall()
    ]


def apply_named_step(step: str, text: str) -> str:
    fn = _STEPS.get(step)
    if fn is None:
        raise NotImplementedError(
            f"matching.yaml names normalisation step {step!r}, which "
            f"conform/text.py does not implement"
        )
    return fn(text)


def normalise(text: str | None, steps: list[str]) -> str:
    """Apply `steps` in order. An unknown step name raises rather than being
    skipped."""
    if not text:
        return ""
    result = text
    for step in steps:
        result = apply_named_step(step, result)
    return result
