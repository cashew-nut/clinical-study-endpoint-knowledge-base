"""Parameter extractors.

Each extractor reads registry text and proposes a value for one axis, always with the
matched span that justified it. Extractors are deliberately conservative: where the
text is ambiguous they return the explicit `unspecified` term rather than a plausible
guess, so that "we do not know" stays distinguishable from "we know it is the default".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

EXTRACTOR_VERSION = "1.0.0"


@dataclass(frozen=True)
class Extraction:
    axis_id: str
    term_id: str
    extractor_id: str
    extractor_version: str
    source_field: str
    matched_text: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class TextFields:
    """The source text an extractor may read, keyed by the field it came from."""

    measure: str
    description: str
    time_frame: str

    def items(self) -> Iterable[tuple[str, str]]:
        yield "measure", self.measure
        yield "description", self.description
        yield "time_frame", self.time_frame

    @property
    def combined(self) -> str:
        return " || ".join(part for part in (self.measure, self.description, self.time_frame) if part)


def _first_match(
    fields: TextFields, pattern: re.Pattern[str], order: tuple[str, ...]
) -> tuple[str, re.Match[str]] | None:
    mapping = dict(fields.items())
    for name in order:
        text = mapping.get(name) or ""
        match = pattern.search(text)
        if match:
            return name, match
    return None


# --------------------------------------------------------------------------- #
# timepoint
# --------------------------------------------------------------------------- #
_DURATION_UNITS: dict[str, str] = {
    "hour": "day",  # normalised below; hours are recorded as fractional days
    "hr": "day",
    "day": "day",
    "d": "day",
    "week": "week",
    "wk": "week",
    "month": "month",
    "mo": "month",
    "year": "year",
    "yr": "year",
}

_UNIT_ALTERNATION = r"hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?"

#: "Week 12", "Day 28", "Month 6" -- the ordinal visit form.
_VISIT_ORDINAL = re.compile(
    rf"\b(?P<unit>week|wk|day|month|mo|year|yr)s?\s*(?P<value>\d+(?:\.\d+)?)\b", re.IGNORECASE
)
#: "12 weeks", "24 months", "up to 5 years" -- the duration form.
_DURATION = re.compile(
    rf"\b(?P<value>\d+(?:\.\d+)?)\s*[- ]?\s*(?P<unit>{_UNIT_ALTERNATION})\b", re.IGNORECASE
)

_ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("randomisation", re.compile(r"\b(?:from|since|after|post)?\s*randomi[sz]ation\b", re.IGNORECASE)),
    ("first_dose", re.compile(r"\b(?:first dose|first administration|first study drug|start of treatment|treatment initiation)\b", re.IGNORECASE)),
    ("baseline_visit", re.compile(r"\bbaseline\b", re.IGNORECASE)),
    ("screening", re.compile(r"\bscreening\b", re.IGNORECASE)),
    ("study_enrolment", re.compile(r"\b(?:enroll?ment|study entry|registration)\b", re.IGNORECASE)),
    ("end_of_treatment", re.compile(r"\b(?:end of treatment|EOT|last dose|treatment discontinuation)\b", re.IGNORECASE)),
    ("end_of_study", re.compile(r"\b(?:end of study|EOS|study completion|final visit)\b", re.IGNORECASE)),
    ("index_event", re.compile(r"\bindex (?:event|procedure|hospitali[sz]ation|admission)\b|\bsymptom onset\b", re.IGNORECASE)),
    ("event_driven", re.compile(r"\buntil\s+(?:approximately\s+)?\d+\s+(?:events|deaths)\b|\bevent[- ]driven\b", re.IGNORECASE)),
)

#: Order matters: the first pattern to hit wins, so specific selection rules are tried
#: before generic ones. "Up to 36 months" is a study duration, not a statement that the
#: endpoint is cumulative, so the cumulative pattern requires an explicit aggregation
#: phrase and sits last.
_SELECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("best_across_timepoints", re.compile(r"\bbest (?:overall )?response\b|\bbest observed\b|\bpeak (?:response|value)\b", re.IGNORECASE)),
    ("sustained_over_window", re.compile(r"\bconfirmed\b|\bsustained\b|\bmaintained for\b|\bpersist\w+ for\b", re.IGNORECASE)),
    ("first_occurrence", re.compile(r"\btime to (?:the )?first\b|\bfirst occurrence\b|\btime to first\b", re.IGNORECASE)),
    ("last_observation", re.compile(r"\blast (?:available )?(?:observation|assessment|visit)\b|\bLOCF\b", re.IGNORECASE)),
    ("all_timepoints_longitudinal", re.compile(r"\bMMRM\b|\brepeated measures\b|\bover time\b", re.IGNORECASE)),
    ("single_timepoint", re.compile(r"\bat (?:week|day|month|year)s?\s*\d+\b", re.IGNORECASE)),
    ("cumulative_over_window", re.compile(
        r"\bcumulative\b|\btotal number\b|\bthroughout\b"
        r"|\b(?:over|during|across) the (?:\w+ ){0,2}(?:period|phase|study|treatment|window)\b"
        r"|\bannuali[sz]ed\b|\bper (?:patient|participant)[- ]year\b",
        re.IGNORECASE)),
)


def extract_timepoint_anchor(fields: TextFields) -> Extraction:
    """Determine what the endpoint's assessment time is measured from."""
    order = ("time_frame", "measure", "description")
    for term_id, pattern in _ANCHOR_PATTERNS:
        found = _first_match(fields, pattern, order)
        if found:
            source_field, match = found
            return Extraction(
                axis_id="timepoint_anchor",
                term_id=term_id,
                extractor_id="timepoint_anchor.pattern",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
            )
    return Extraction(
        axis_id="timepoint_anchor",
        term_id="unspecified",
        extractor_id="timepoint_anchor.pattern",
        extractor_version=EXTRACTOR_VERSION,
        source_field="time_frame",
    )


def extract_timepoint_offset(fields: TextFields) -> Extraction | None:
    """Extract the numeric offset and its unit, preferring the visit-ordinal form.

    "Week 12" and "12 weeks" both appear, and the ordinal form is the more reliable
    signal of a nominated visit, so it is tried first.
    """
    order = ("time_frame", "measure", "description")
    for pattern in (_VISIT_ORDINAL, _DURATION):
        found = _first_match(fields, pattern, order)
        if not found:
            continue
        source_field, match = found
        raw_unit = match.group("unit").lower().rstrip("s")
        unit = _DURATION_UNITS.get(raw_unit)
        if unit is None:
            continue
        value = float(match.group("value"))
        if raw_unit in {"hour", "hr"}:
            value = value / 24.0
        return Extraction(
            axis_id="timepoint_offset",
            term_id=unit,
            extractor_id="timepoint_offset.pattern",
            extractor_version=EXTRACTOR_VERSION,
            source_field=source_field,
            matched_text=match.group(0),
            span_start=match.start(),
            span_end=match.end(),
            value_num=value,
            unit=unit,
        )
    return None


def extract_timepoint_selection(fields: TextFields) -> Extraction:
    order = ("measure", "description", "time_frame")
    for term_id, pattern in _SELECTION_PATTERNS:
        found = _first_match(fields, pattern, order)
        if found:
            source_field, match = found
            return Extraction(
                axis_id="timepoint_selection",
                term_id=term_id,
                extractor_id="timepoint_selection.pattern",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
            )
    return Extraction(
        axis_id="timepoint_selection",
        term_id="unspecified",
        extractor_id="timepoint_selection.pattern",
        extractor_version=EXTRACTOR_VERSION,
        source_field="measure",
    )


# --------------------------------------------------------------------------- #
# threshold
# --------------------------------------------------------------------------- #
_GTE = r"(?:>=|≥|at least|no less than|greater than or equal to|minimum of)"
_LTE = r"(?:<=|≤|at most|no more than|less than or equal to)"
_LT = r"(?:<|below|less than|under)"
_GT = r"(?:>|above|more than|greater than)"

#: "at least a 20% improvement", "≥30% decrease", "30% reduction"
_RELATIVE = re.compile(
    rf"(?P<op>{_GTE}|{_GT})?\s*(?:an?\s+)?(?P<value>\d+(?:\.\d+)?)\s*%\s*"
    r"(?P<direction>improvement|reduction|decrease|decline|increase|gain|rise|change)",
    re.IGNORECASE,
)
#: "improvement of at least 20%", "reduction of ≥30%"
_RELATIVE_SUFFIX = re.compile(
    rf"(?P<direction>improvement|reduction|decrease|decline|increase|gain|rise)\s+of\s+"
    rf"(?:at least\s+)?(?P<op>{_GTE}|{_GT})?\s*(?P<value>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
#: "< 7%", "below 7.0%", "HbA1c <7%"
_ABSOLUTE = re.compile(
    rf"(?P<op>{_LT}|{_LTE}|{_GTE}|{_GT})\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>%|mg/dL|mmol/L|mmHg|letters?|points?)?",
    re.IGNORECASE,
)

_UNIT_TO_TERM = {
    "%": "percent",
    "mg/dl": "mg_per_dl",
    "mmol/l": "mmol_per_l",
    "mmhg": "mmhg",
    "letter": "letters_etdrs",
    "letters": "letters_etdrs",
    "point": "score_points",
    "points": "score_points",
}

_DECREASE_WORDS = {"reduction", "decrease", "decline"}
_INCREASE_WORDS = {"increase", "gain", "rise", "improvement"}


def _operator_for(op_text: str | None, direction: str | None) -> str:
    if direction:
        lowered = direction.lower()
        if lowered in _DECREASE_WORDS:
            return "decrease_by_at_least"
        if lowered in _INCREASE_WORDS:
            return "increase_by_at_least"
    if not op_text:
        return "gte"
    lowered = op_text.lower()
    if re.fullmatch(_LT, lowered, re.IGNORECASE):
        return "lt"
    if re.fullmatch(_LTE, lowered, re.IGNORECASE):
        return "lte"
    if re.fullmatch(_GT, lowered, re.IGNORECASE):
        return "gt"
    return "gte"


def extract_threshold(fields: TextFields) -> list[Extraction]:
    """Extract a responder threshold, if the text states one.

    Returns extractions for threshold_kind and threshold_operator, carrying the numeric
    value and unit. An empty list means the text stated no threshold, which is the
    common case: most thresholds live in the concept definition rather than the title.
    """
    order = ("measure", "description")
    for pattern, kind in ((_RELATIVE, "relative_change_percent"), (_RELATIVE_SUFFIX, "relative_change_percent")):
        found = _first_match(fields, pattern, order)
        if not found:
            continue
        source_field, match = found
        groups = match.groupdict()
        operator = _operator_for(groups.get("op"), groups.get("direction"))
        value = float(groups["value"])
        return [
            Extraction(
                axis_id="threshold_kind",
                term_id=kind,
                extractor_id="threshold.relative",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
                value_num=value,
                unit="percent",
            ),
            Extraction(
                axis_id="threshold_operator",
                term_id=operator,
                extractor_id="threshold.relative",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
                value_num=value,
                unit="percent",
            ),
        ]

    found = _first_match(fields, _ABSOLUTE, order)
    if found:
        source_field, match = found
        groups = match.groupdict()
        operator = _operator_for(groups.get("op"), None)
        unit_text = (groups.get("unit") or "").lower()
        unit_term = _UNIT_TO_TERM.get(unit_text)
        return [
            Extraction(
                axis_id="threshold_kind",
                term_id="absolute_value",
                extractor_id="threshold.absolute",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
                value_num=float(groups["value"]),
                unit=unit_term,
            ),
            Extraction(
                axis_id="threshold_operator",
                term_id=operator,
                extractor_id="threshold.absolute",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
                value_num=float(groups["value"]),
                unit=unit_term,
            ),
        ]
    return []


# --------------------------------------------------------------------------- #
# analysis population
# --------------------------------------------------------------------------- #
_POPULATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("modified_itt", re.compile(r"\bm-?ITT\b|\bmodified intent(?:ion)?[- ]to[- ]treat\b", re.IGNORECASE)),
    ("full_analysis_set", re.compile(r"\bfull analysis set\b|\bFAS\b", re.IGNORECASE)),
    ("itt", re.compile(r"\bITT\b|\bintent(?:ion)?[- ]to[- ]treat\b", re.IGNORECASE)),
    ("per_protocol", re.compile(r"\bper[- ]protocol\b|\bPP (?:set|population)\b", re.IGNORECASE)),
    ("safety", re.compile(r"\bsafety (?:population|set|analysis set)\b|\bas[- ]treated\b", re.IGNORECASE)),
    ("evaluable", re.compile(r"\b(?:response |efficacy )?evaluable\b", re.IGNORECASE)),
    ("randomised", re.compile(r"\ball randomi[sz]ed\b", re.IGNORECASE)),
)


def extract_analysis_population(fields: TextFields) -> Extraction:
    order = ("description", "measure", "time_frame")
    for term_id, pattern in _POPULATION_PATTERNS:
        found = _first_match(fields, pattern, order)
        if found:
            source_field, match = found
            return Extraction(
                axis_id="analysis_population",
                term_id=term_id,
                extractor_id="analysis_population.pattern",
                extractor_version=EXTRACTOR_VERSION,
                source_field=source_field,
                matched_text=match.group(0),
                span_start=match.start(),
                span_end=match.end(),
            )
    return Extraction(
        axis_id="analysis_population",
        term_id="unspecified",
        extractor_id="analysis_population.pattern",
        extractor_version=EXTRACTOR_VERSION,
        source_field="description",
    )


#: Extractors that always run, each returning exactly one extraction.
SINGLE_EXTRACTORS: tuple[Callable[[TextFields], Extraction], ...] = (
    extract_timepoint_anchor,
    extract_timepoint_selection,
    extract_analysis_population,
)


def run_all(fields: TextFields) -> dict[str, Any]:
    """Run every extractor, returning axis extractions plus the numeric offset."""
    extractions: list[Extraction] = [fn(fields) for fn in SINGLE_EXTRACTORS]
    extractions.extend(extract_threshold(fields))
    offset = extract_timepoint_offset(fields)
    return {"extractions": extractions, "offset": offset}
