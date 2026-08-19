"""Text normalisation per matching.yaml's `normalisation` list.

matching.yaml states the steps and their order; this module supplies the one
thing a vocab file cannot -- the code that performs each named step. Which
steps run, and in what order, is read from `vocab.matching_normalisation`
(written by `vocab validate` from matching.yaml), not hardcoded here.

`casefold_unless_case_sensitive` is deliberately a no-op in this module: rather
than casefold the text and lose the ability to apply matching.yaml's
case-sensitive short-acronym rule, this pipeline keeps the text case-preserved
throughout and lets each compiled regex carry its own case sensitivity
(`re.IGNORECASE` for ordinary synonyms/patterns, no flag for a short all-caps
acronym) -- see `conform/matching.py`. That is equivalent in effect (a
case-preserved string matched with IGNORECASE reads the same as a casefolded
string matched without it) and is the only way both matching.yaml rules can be
satisfied by one prepared string.
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
    return text  # see module docstring: handled via regex flags instead


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
    """Apply matching.yaml's normalisation steps, in the order given, to `text`.
    Every step in `steps` must be one this module implements -- an unrecognised
    step name fails loudly rather than being silently skipped, so a new
    normalisation step added to matching.yaml is a build failure here, not a
    silent no-op in the pipeline."""
    if not text:
        return ""
    result = text
    for step in steps:
        result = apply_named_step(step, result)
    return result
