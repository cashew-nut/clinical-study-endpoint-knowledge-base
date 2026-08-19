"""timepoint_patterns.yaml's parser: preprocessing -> not_if_matches guards ->
patterns in priority order -> named-group extraction, exactly as the task
requires. Every step name, abbreviation/typo/numeral dict, and pattern comes
from `vocab.timepoint_*` (written by `vocab validate`); only the text-
transformation CODE for each named preprocessing step lives here, the same
division of labour as conform/text.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import duckdb

from clinical_endpoints.conform.text import apply_named_step

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_DIGITS_RE = re.compile(r"\d+$")


@dataclass(frozen=True)
class TimepointTerm:
    term_id: str
    priority: int
    guard_res: tuple[re.Pattern, ...]
    pattern_res: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class TimepointRules:
    preprocessing_steps: tuple[str, ...]
    abbreviations: dict[str, str]
    common_typos: dict[str, str]
    numeral_words: dict[str, int]
    unit_alternation: str  # e.g. "minutes|minute|hours|hour|..."
    terms: tuple[TimepointTerm, ...]  # priority order
    fallback: str  # vocab.matching_cascade[timepoint].fallback, e.g. 'unspecified'
    disambiguation: tuple[dict, ...]


@dataclass(frozen=True)
class TimepointResult:
    pattern_id: str
    raw: str
    prepared: str
    extracted: dict
    match_method: Optional[str]  # 'exact' when a real pattern hit, else None


def load_timepoint_rules(con: duckdb.DuckDBPyConnection) -> TimepointRules:
    preprocessing = [
        row[0] for row in con.execute("SELECT step FROM vocab.timepoint_preprocessing ORDER BY ordinal").fetchall()
    ]
    abbreviations = dict(con.execute("SELECT abbreviation, expansion FROM vocab.timepoint_abbreviations").fetchall())
    typos = dict(con.execute("SELECT typo, correction FROM vocab.timepoint_common_typos").fetchall())
    numerals = dict(con.execute("SELECT word, value FROM vocab.timepoint_numeral_words").fetchall())
    unit_tokens = [row[0] for row in con.execute("SELECT token FROM vocab.timepoint_unit_tokens").fetchall()]
    unit_alternation = "|".join(re.escape(t) for t in sorted(set(unit_tokens), key=len, reverse=True))

    guard_rows: dict[str, list[str]] = {}
    match_rows: dict[str, list[str]] = {}
    for term_id, pattern, role in con.execute(
        "SELECT term_id, pattern, pattern_role FROM vocab.patterns WHERE dimension = 'timepoint_pattern' "
        "ORDER BY term_id, ordinal"
    ).fetchall():
        target = guard_rows if role == "guard" else match_rows
        target.setdefault(term_id, []).append(pattern)

    terms = []
    for term_id, priority in con.execute("SELECT id, priority FROM vocab.timepoint_patterns ORDER BY priority").fetchall():
        terms.append(
            TimepointTerm(
                term_id=term_id,
                priority=int(priority),
                guard_res=tuple(re.compile(p, re.IGNORECASE) for p in guard_rows.get(term_id, [])),
                pattern_res=tuple(re.compile(p, re.IGNORECASE) for p in match_rows.get(term_id, [])),
            )
        )

    fallback_row = con.execute(
        "SELECT fallback_value FROM vocab.matching_cascade WHERE dimension = 'timepoint' AND fallback_value IS NOT NULL"
    ).fetchone()
    fallback = fallback_row[0] if fallback_row else "unspecified"

    disambiguation = [
        {"between": between, "prefer": prefer, "otherwise": otherwise, "if_form_in": set(if_form_in)}
        for _ordinal, between, prefer, otherwise, if_form_in in con.execute(
            "SELECT ordinal, between_forms, prefer, otherwise, if_form_in "
            "FROM vocab.timepoint_disambiguation ORDER BY ordinal"
        ).fetchall()
    ]

    return TimepointRules(
        preprocessing_steps=tuple(preprocessing),
        abbreviations=abbreviations,
        common_typos=typos,
        numeral_words={k: int(v) for k, v in numerals.items()},
        unit_alternation=unit_alternation,
        terms=tuple(terms),
        fallback=fallback,
        disambiguation=tuple(disambiguation),
    )


# --------------------------------------------------------------- preprocessing


def _drop_uninformative_parentheticals(text: str, unit_alt: str) -> str:
    has_unit_digit = re.compile(rf"\d+\s*(?:{unit_alt})|(?:{unit_alt})\s*\d+", re.IGNORECASE)
    while True:
        m = re.search(r"\s*\([^()]*\)", text)
        if not m:
            return text
        without = f"{text[:m.start()]} {text[m.end():]}"
        if not has_unit_digit.search(without):
            return text  # stripping would delete the only horizon in the string
        text = _WHITESPACE_RE.sub(" ", without).strip()


def _split_hyphenated_units(text: str, unit_alt: str) -> str:
    text = re.sub(rf"\b({unit_alt})-\s*(\d)", r"\1 \2", text, flags=re.IGNORECASE)
    text = re.sub(rf"(\d)\s*-\s*({unit_alt})\b", r"\1 \2", text, flags=re.IGNORECASE)
    return text


def _normalise_ordinal_timepoints(text: str, unit_alt: str) -> str:
    text = re.sub(
        rf"(\d+)(?:st|nd|rd|th)\s+({unit_alt})\b",
        lambda m: f"{m.group(2)} {m.group(1)}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    return text


def _normalise_plus_minus(text: str) -> str:
    return re.sub(r"\+\s*/?\s*-", "±", text)


def _normalise_list_separators(text: str) -> str:
    for ch in ("、", "，", "\\"):
        text = text.replace(ch, ",")
    return text


def _expand_dict(text: str, mapping: dict[str, str]) -> str:
    # Longest key first so a compound entry ("twenty-four") is substituted
    # before a shorter entry it contains ("four") can pre-empt it.
    for word in sorted(mapping, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(word)}\b", str(mapping[word]), text, flags=re.IGNORECASE)
    return text


_STEP_FUNCTIONS = {
    "drop_uninformative_parentheticals": lambda text, rules: _drop_uninformative_parentheticals(text, rules.unit_alternation),
    "split_hyphenated_units": lambda text, rules: _split_hyphenated_units(text, rules.unit_alternation),
    "normalise_ordinal_timepoints": lambda text, rules: _normalise_ordinal_timepoints(text, rules.unit_alternation),
    "collapse_whitespace": lambda text, rules: apply_named_step("collapse_whitespace", text),
    "normalise_unicode_dashes": lambda text, rules: apply_named_step("normalise_unicode_dashes", text),
    "normalise_plus_minus": lambda text, rules: _normalise_plus_minus(text),
    "normalise_list_separators": lambda text, rules: _normalise_list_separators(text),
    "strip_trailing_punctuation": lambda text, rules: text.rstrip(".;").strip(),
    "expand_numeral_words": lambda text, rules: _expand_dict(text, rules.numeral_words),
    # common_typos has no preprocessing step of its own in timepoint_patterns.yaml;
    # bundled here since it is the same class of whole-token substitution.
    "expand_abbreviations": lambda text, rules: _expand_dict(_expand_dict(text, rules.common_typos), rules.abbreviations),
    "match_case_insensitively": lambda text, rules: text,  # patterns already run with IGNORECASE
}


def prepare(text: str, rules: TimepointRules) -> str:
    prepared = text or ""
    for step in rules.preprocessing_steps:
        fn = _STEP_FUNCTIONS.get(step)
        if fn is None:
            raise NotImplementedError(f"timepoint_patterns.yaml names preprocessing step {step!r}, which conform/timepoint.py does not implement")
        prepared = fn(prepared, rules)
    return prepared


def _extracted_groups(match: re.Match) -> dict:
    out = {}
    for key, value in match.groupdict().items():
        if value is None:
            continue
        canonical = _TRAILING_DIGITS_RE.sub("", key) or key
        out.setdefault(canonical, value)
    return out


def classify(text: Optional[str], rules: TimepointRules) -> TimepointResult:
    raw = text or ""
    prepared = prepare(raw, rules)
    for term in rules.terms:
        if any(g.search(prepared) for g in term.guard_res):
            continue
        for pattern in term.pattern_res:
            m = pattern.search(prepared)
            if m:
                return TimepointResult(term.term_id, raw, prepared, _extracted_groups(m), "exact")
    return TimepointResult(rules.fallback, raw, prepared, {}, None)


def _connective_signals_window(prepared_text: str) -> bool:
    return bool(re.search(r"\bthrough\b|\bup to\b", prepared_text, re.IGNORECASE))


def apply_disambiguation(result: TimepointResult, form_id: Optional[str], rules: TimepointRules) -> TimepointResult:
    """timepoint_patterns.yaml's `disambiguation`: the resolved FORM can
    override a baseline_to_timepoint/cumulative_window call when the string is
    genuinely ambiguous (a baseline token plus a "through"/"up to" connective
    and exactly one horizon) -- see that file's `connective_evidence`."""
    for rule in rules.disambiguation:
        if result.pattern_id != rule["otherwise"] or result.pattern_id not in rule["between"]:
            continue
        if form_id in rule["if_form_in"] and _connective_signals_window(result.prepared):
            return TimepointResult(rule["prefer"], result.raw, result.prepared, result.extracted, result.match_method)
    return result
