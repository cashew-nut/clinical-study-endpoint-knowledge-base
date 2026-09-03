"""D6's other half: the free-text `unit_of_measure` a results row carries,
resolved against `scales.yaml`.

On the protocol side the scale is inferred -- from an explicit unit in the
text, else the form's typical scale, else the measurement's default (see
scales.yaml's header). On the results side the registry states it outright, in
a field whose *entire content* is the unit: "L", "mL", "months", "mg/dL",
"percentage of participants". That difference is what this module exists for.

It means a whole-string comparison is available here that is not available in
prose, and that matters: matching.yaml sets `min_synonym_length: 2`, so the
generic matcher deliberately never matches the single-character synonym "L"
inside a sentence (too many false friends). As the entire content of the unit
field, "L" is unambiguous. So the cascade is:

1. the whole field, compared to the scale synonyms  -> `exact`
2. the whole field with a trailing parenthetical removed ("Liters (L)")
   -> `exact`
3. the generic in-prose matcher over the field       -> `syntactic_rule`
4. scales.yaml's own `default_when_unmatched`        -> no match method

Step 4 resolves to `not_stated` rather than to NULL, exactly as the vocabulary
declares, so an unresolved unit stays countable -- which is what makes gate
question 4 ("what share of results units normalise against scales.yaml as it
stands") answerable at all.
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
    #: 'exact' | 'syntactic_rule' | None (the declared fallback fired)
    match_method: Optional[str]


@dataclass(frozen=True)
class UnitRules:
    """Everything `resolve_unit` and `to_si` need, loaded once per run."""

    #: normalised synonym -> scale id, for the case-insensitive comparison.
    by_synonym: dict[str, str]
    #: the same, keyed on the un-casefolded synonym, for short all-caps
    #: acronyms ("L", "mL", "IU/L") where matching.yaml's case-sensitivity rule
    #: applies and lower-cased "l" must not win.
    by_case_sensitive_synonym: dict[str, str]
    matcher: matcher_mod.TermMatcher
    normalisation_steps: list[str]
    default_scale_id: str
    #: scale id -> (si scale id, multiplicative factor), for the terms where
    #: scales.yaml declares one. Absent means "not convertible", which is a
    #: deliberate statement for mg/dL <-> mmol/L (needs a molar mass) and for
    #: HbA1c's NGSP <-> IFCC (affine, not a factor).
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
    """matching.yaml's case-sensitivity rule, applied to a whole-field
    comparison: a short all-caps synonym must be written in capitals to
    match ("L" is litres, "l" is a letter), everything else is
    case-insensitive."""
    exact = rules.by_case_sensitive_synonym.get(text)
    if exact is not None:
        return exact
    lowered = text.lower()
    owner = rules.by_synonym.get(lowered)
    if owner is None:
        return None
    # The registry wrote it in some other case; refuse only where the
    # vocabulary's own spelling is a short acronym whose capitals are the
    # evidence it was meant.
    for candidate, term_id in rules.by_case_sensitive_synonym.items():
        if candidate.lower() == lowered and term_id == owner and _is_short_acronym(candidate):
            return None
    return owner


def resolve_unit(raw: Optional[str], rules: UnitRules) -> UnitResolution:
    """A results-section `unit_of_measure` -> a scale id and how it was decided."""
    text = normalise(raw, rules.normalisation_steps)
    if not text:
        return UnitResolution(rules.default_scale_id, None)

    hit = _lookup_whole(text, rules)
    if hit:
        return UnitResolution(hit, "exact")

    # "Liters (L)" and "Percentage of participants (%)" -- the registry's
    # habit of restating the symbol. Try the parenthetical, then the stem.
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
    """`value`, expressed in `scale_id`, converted to that family's canonical
    unit -- or (None, None) where scales.yaml declares no factor.

    Valid for a standard deviation as well as for a measurement: an SD scales
    by |a| under any y = a*x + b, so a purely multiplicative factor carries it
    across unchanged. That is exactly why the affine pair (HbA1c NGSP vs IFCC)
    carries no `factor_to_si` -- and why nothing here tries to invent one.
    """
    if scale_id is None or value is None:
        return None, None
    entry = rules.si.get(scale_id)
    if entry is None:
        return None, None
    si_scale_id, factor = entry
    return value * factor, si_scale_id
