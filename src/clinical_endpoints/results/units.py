"""Resolve a results row's free-text `unit_of_measure` against scales.yaml.

The field's entire content is the unit ("L", "mL", "months"), so a
whole-string comparison is available that in-prose matching is not: the
generic matcher never matches the single-character synonym "L" inside a
sentence, but as a whole field it is unambiguous. Cascade:

1. the whole field against the scale synonyms            -> `exact`
2. the same with a trailing parenthetical split off       -> `exact`
3. the generic in-prose matcher                          -> `syntactic_rule`
4. scales.yaml's `default_when_unmatched` (not_stated)   -> None
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import duckdb

from clinical_endpoints.conform import matcher as matcher_mod
from clinical_endpoints.conform.text import normalise

_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


@dataclass(frozen=True)
class UnitResolution:
    scale_id: str
    match_method: Optional[str]  # 'exact' | 'syntactic_rule' | None


@dataclass(frozen=True)
class UnitRules:
    by_synonym: dict[str, str]  # lower-cased synonym -> scale id
    by_case_sensitive_synonym: dict[str, str]  # as written, for short all-caps acronyms
    matcher: matcher_mod.TermMatcher
    normalisation_steps: list[str]
    default_scale_id: str
    # scale id -> (si scale id, factor). Absent means not convertible, which is
    # deliberate for mg/dL <-> mmol/L (needs a molar mass) and HbA1c NGSP <-> IFCC (affine).
    si: dict[str, tuple[str, float]]
    kind: dict[str, Optional[str]]


def load_unit_rules(
    con: duckdb.DuckDBPyConnection, *, default_scale_id: str = "not_stated"
) -> UnitRules:
    settings = matcher_mod.load_settings(con)
    steps = [
        row[0]
        for row in con.execute(
            "SELECT step FROM vocab.matching_normalisation ORDER BY ordinal"
        ).fetchall()
    ]

    by_synonym: dict[str, str] = {}
    by_case_sensitive: dict[str, str] = {}
    for term_id, synonym in con.execute(
        "SELECT term_id, synonym FROM vocab.synonyms WHERE dimension = 'scale'"
    ).fetchall():
        prepared = normalise(synonym, steps)
        if not prepared:
            continue
        by_case_sensitive.setdefault(prepared, term_id)
        by_synonym.setdefault(prepared.lower(), term_id)

    si: dict[str, tuple[str, float]] = {}
    kinds: dict[str, Optional[str]] = {}
    for scale_id, kind, si_equivalent, factor in con.execute(
        "SELECT id, kind, si_equivalent, factor_to_si FROM vocab.scales"
    ).fetchall():
        kinds[scale_id] = kind
        if si_equivalent and factor is not None:
            si[scale_id] = (si_equivalent, float(factor))

    return UnitRules(
        by_synonym=by_synonym,
        by_case_sensitive_synonym=by_case_sensitive,
        matcher=matcher_mod.build_matcher(con, "scale", "scales", settings),
        normalisation_steps=steps,
        default_scale_id=default_scale_id,
        si=si,
        kind=kinds,
    )


def _is_short_acronym(text: str) -> bool:
    return text.isupper() and any(c.isalpha() for c in text) and len(text) <= 5


def _lookup_whole(text: str, rules: UnitRules) -> Optional[str]:
    """Whole-field comparison under matching.yaml's case rule: a short all-caps
    synonym must be written in capitals ("L" is litres, "l" is a letter)."""
    exact = rules.by_case_sensitive_synonym.get(text)
    if exact is not None:
        return exact
    lowered = text.lower()
    owner = rules.by_synonym.get(lowered)
    if owner is None:
        return None
    for candidate, term_id in rules.by_case_sensitive_synonym.items():
        if candidate.lower() == lowered and term_id == owner and _is_short_acronym(candidate):
            return None
    return owner


def resolve_unit(raw: Optional[str], rules: UnitRules) -> UnitResolution:
    text = normalise(raw, rules.normalisation_steps)
    if not text:
        return UnitResolution(rules.default_scale_id, None)

    hit = _lookup_whole(text, rules)
    if hit:
        return UnitResolution(hit, "exact")

    # "Liters (L)": try the parenthetical, then the stem.
    parenthetical = _TRAILING_PARENTHETICAL_RE.search(text)
    if parenthetical:
        inner = parenthetical.group(1).strip()
        stem = _TRAILING_PARENTHETICAL_RE.sub("", text).strip()
        for candidate in (inner, stem):
            hit = _lookup_whole(candidate, rules) if candidate else None
            if hit:
                return UnitResolution(hit, "exact")

    match = rules.matcher.match(text)
    if match:
        return UnitResolution(match.term_id, "syntactic_rule")
    return UnitResolution(rules.default_scale_id, None)


def to_si(scale_id: Optional[str], value: Optional[float], rules: UnitRules):
    """`value` in `scale_id` converted to the family's canonical unit, or
    (None, None) where no factor is declared. Valid for an SD as well as a
    measurement, since an SD scales by |a| under y = a*x + b."""
    if scale_id is None or value is None:
        return None, None
    entry = rules.si.get(scale_id)
    if entry is None:
        return None, None
    si_scale_id, factor = entry
    return value * factor, si_scale_id
