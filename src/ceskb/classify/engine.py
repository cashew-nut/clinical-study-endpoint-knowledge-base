"""Classification engine: registry outcome text to Layer B endpoint specifications.

Precedence is fixed and explicit, because the value of this layer is that a reader can
always tell where a parameter came from:

  1. defining axes (form, measurement, reference, direction, scale) come from the
     canonical concept, and only a rule may override them -- a regex over a title is
     never allowed to redefine what an endpoint fundamentally is;
  2. default axes (summary measure, timepoint selection) come from the concept, may be
     overridden by a rule, and may then be overridden by an extractor;
  3. operational axes (timepoint anchor, analysis population, thresholds) come from
     extractors, because they are protocol choices that only the study text can supply;
  4. a reviewer's override beats all of them, including the defining axes. The
     asymmetry against rule 1 is deliberate: a regex over a title must never be
     allowed to redefine an endpoint, and a human who has read the protocol must.

Every resulting value carries its origin, and every rule and extractor match carries
the span of text that produced it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

import duckdb

from ceskb.classify.extractors import Extraction, TextFields, run_all
from ceskb.config import DERIVATION_VERSION
from ceskb.review.overrides import Override, active_overrides
from ceskb.store.db import _json, finish_run, start_run, utcnow
from ceskb.vocab.loader import STRUCTURE_AXES, Rule, Vocabulary, load_vocabulary

#: Axes whose values an extractor may not override -- see module docstring.
DEFINING_AXES = frozenset(
    axis for axis, role in STRUCTURE_AXES.values() if role == "defining"
)

#: Threshold kinds a free-text extractor cannot restate faithfully, so the curated
#: concept definition wins. A title saying "ACR20" does not mean "at least 20 percent"
#: of any single quantity; it means a seven-component criteria set.
CONCEPT_AUTHORITATIVE_THRESHOLD_KINDS = frozenset(
    {"composite_criteria", "category_attainment", "event_occurrence"}
)

#: Axes that only ever come from extraction.
OPERATIONAL_AXES = ("timepoint_anchor", "analysis_population")


@dataclass
class RuleMatch:
    rule: Rule
    source_field: str
    matched_text: str
    span_start: int
    span_end: int
    selected: bool = False


@dataclass
class OutcomeRecord:
    outcome_uid: str
    study_id: str
    endpoint_level: str
    measure: str
    description: str
    time_frame: str
    therapeutic_areas: tuple[str, ...] = ()


@dataclass
class EndpointSpec:
    spec_id: str
    outcome: OutcomeRecord
    concept_id: str
    match_confidence: float
    selected_rule_id: str | None
    competing_rule_count: int
    axes: dict[str, tuple[str, str, str | None]] = field(default_factory=dict)
    timepoint_value: float | None = None
    timepoint_unit: str | None = None
    timepoint_raw: str | None = None
    threshold_value: float | None = None
    threshold_unit: str | None = None
    threshold_raw: str | None = None
    unresolved_axes: list[str] = field(default_factory=list)
    rule_matches: list[RuleMatch] = field(default_factory=list)
    extractions: list[Extraction] = field(default_factory=list)
    #: The winning rule tied with another on both priority and confidence, so the
    #: choice between them was arbitrary. Surfaced rather than swallowed.
    ambiguous_tie: bool = False
    overridden: bool = False


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _field_text(fields: TextFields, name: str) -> str:
    if name == "measure":
        return fields.measure
    if name == "description":
        return fields.description
    if name == "time_frame":
        return fields.time_frame
    if name == "measure_and_description":
        return f"{fields.measure} {fields.description}".strip()
    return ""


def evaluate_rule(rule: Rule, fields: TextFields) -> RuleMatch | None:
    """Return the match that fired this rule, or None.

    A rule fires only when every clause it declares holds. The span reported is the
    first `any_of` or `all_of` hit, which is what a reviewer wants to see highlighted.
    """
    spec = rule.match
    field_names = spec.get("fields") or ["measure"]
    evidence: RuleMatch | None = None

    for field_name in field_names:
        text = _field_text(fields, field_name)
        if not text:
            continue

        if any(_compile(p).search(text) for p in spec.get("none_of", [])):
            return None

        any_of = spec.get("any_of")
        any_match: re.Match[str] | None = None
        if any_of:
            for pattern in any_of:
                any_match = _compile(pattern).search(text)
                if any_match:
                    break
            if not any_match:
                continue

        all_of = spec.get("all_of")
        all_match: re.Match[str] | None = None
        if all_of:
            matches = [_compile(p).search(text) for p in all_of]
            if not all(matches):
                continue
            all_match = matches[0]

        if not any_of and not all_of:
            continue

        hit = any_match or all_match
        if hit is None:
            continue
        evidence = RuleMatch(
            rule=rule,
            source_field=field_name,
            matched_text=hit.group(0),
            span_start=hit.start(),
            span_end=hit.end(),
        )
        break

    if evidence is None:
        return None

    # none_of is re-checked across every declared field so a disqualifier in the
    # description cannot be evaded by matching on the measure alone.
    for field_name in field_names:
        text = _field_text(fields, field_name)
        if text and any(_compile(p).search(text) for p in spec.get("none_of", [])):
            return None

    return evidence


def _applicable(rule: Rule, outcome: OutcomeRecord) -> bool:
    if rule.therapeutic_area_hint is None:
        return True
    return rule.therapeutic_area_hint in outcome.therapeutic_areas


def classify_outcome(
    outcome: OutcomeRecord, vocab: Vocabulary, override: Override | None = None
) -> EndpointSpec | None:
    """Classify one outcome.

    Returns None when no rule fires, and also when a reviewer has ruled that no concept
    in the vocabulary describes this outcome -- a suppressed match is genuinely
    unclassified, and reporting it as such keeps coverage honest rather than counting a
    match a human has rejected.
    """
    if override is not None and override.suppresses:
        return None

    fields = TextFields(
        measure=outcome.measure or "",
        description=outcome.description or "",
        time_frame=outcome.time_frame or "",
    )

    matches = [
        match
        for rule in vocab.rules
        if _applicable(rule, outcome)
        for match in (evaluate_rule(rule, fields),)
        if match is not None
    ]

    # A reviewer naming a concept is itself sufficient grounds for a specification.
    # Without this, the single most valuable correction -- "no rule fires but this is
    # plainly ORR" -- could not be expressed, and coverage could only ever be raised
    # by writing another regex.
    if not matches and (override is None or not override.changes_concept):
        return None

    if matches:
        matches.sort(key=lambda m: (-m.rule.priority, -m.rule.confidence, m.rule.rule_id))
        winner: RuleMatch | None = matches[0]
        winner.selected = True
        concept = vocab.concept(winner.rule.concept_id)
        # Sorting is total, so a tie is always broken -- by rule_id, alphabetically.
        # That is a fine way to stay deterministic and a terrible way to be right, so
        # record when it happened and let the review queue pick it up.
        runner_up = matches[1] if len(matches) > 1 else None
        ambiguous_tie = runner_up is not None and (
            runner_up.rule.priority == winner.rule.priority
            and runner_up.rule.confidence == winner.rule.confidence
            and runner_up.rule.concept_id != winner.rule.concept_id
        )
    else:
        winner = None
        concept = vocab.concept(override.concept_id)  # type: ignore[arg-type]
        ambiguous_tie = False

    spec = EndpointSpec(
        spec_id=uuid.uuid5(
            uuid.NAMESPACE_URL, f"ceskb:{outcome.outcome_uid}:{DERIVATION_VERSION}"
        ).hex,
        outcome=outcome,
        concept_id=concept.concept_id,
        match_confidence=winner.rule.confidence if winner else 1.0,
        selected_rule_id=winner.rule.qualified_id if winner else None,
        competing_rule_count=max(len(matches) - 1, 0),
        rule_matches=matches,
        ambiguous_tie=ambiguous_tie,
    )

    # 1. concept structure
    for key, term_id in concept.structure.items():
        axis_id, _role = STRUCTURE_AXES[key]
        spec.axes[axis_id] = (term_id, "concept_default", f"concept {concept.concept_id}")

    # 2. rule assertions
    if winner is not None:
        for axis_id, term_id in winner.rule.asserts.items():
            if isinstance(term_id, str) and axis_id in vocab.axes:
                spec.axes[axis_id] = (term_id, "rule_assert", winner.rule.qualified_id)

    # 3. concept definitional threshold, before extraction so extraction can win where
    #    the threshold is a convention rather than part of the definition
    concept_threshold = concept.definitional_threshold
    if concept_threshold:
        spec.axes["threshold_kind"] = (
            concept_threshold["kind"],
            "concept_default",
            f"concept {concept.concept_id}",
        )
        spec.axes["threshold_operator"] = (
            concept_threshold["operator"],
            "concept_default",
            f"concept {concept.concept_id}",
        )
        value = concept_threshold["value"]
        spec.threshold_value = float(value) if isinstance(value, (int, float)) else None
        spec.threshold_unit = concept_threshold.get("unit")
        spec.threshold_raw = str(value)

    # 4. extraction
    results = run_all(fields)
    extractions: list[Extraction] = results["extractions"]
    offset: Extraction | None = results["offset"]
    spec.extractions = list(extractions)

    threshold_locked = bool(
        concept_threshold
        and concept_threshold["kind"] in CONCEPT_AUTHORITATIVE_THRESHOLD_KINDS
    )

    # An extraction that rediscovers the number the concept already declares carries no
    # new information, and letting it write back its own reading loses detail. Read
    # naively, "gaining at least 15 letters" is a threshold on a value; the concept
    # knows it is a threshold on *change* from the participant's own baseline. Only a
    # value that differs from the convention is evidence this study departed from it.
    threshold_confirmed = bool(
        concept_threshold
        and spec.threshold_value is not None
        and any(
            e.axis_id == "threshold_kind"
            and e.value_num is not None
            and abs(e.value_num - spec.threshold_value) < 1e-9
            for e in extractions
        )
    )

    # A time-to-event endpoint selects the first qualifying event by construction, so
    # a phrase in the time frame must not be allowed to restate that as something else.
    # Where a title really describes a landmark rate, a rule asserts the form instead.
    selection_locked = spec.axes.get("endpoint_form", ("", "", None))[0] == "time_to_event"

    for extraction in extractions:
        axis_id = extraction.axis_id
        if axis_id in DEFINING_AXES:
            continue  # extractors never redefine what the endpoint is
        if axis_id == "timepoint_selection" and selection_locked:
            continue
        if axis_id in {"threshold_kind", "threshold_operator"} and (
            threshold_locked or threshold_confirmed
        ):
            continue
        if extraction.term_id == "unspecified" and axis_id in spec.axes:
            continue  # do not overwrite a known value with "we could not tell"
        # An extractor that reached "unspecified" did not resolve anything, and saying
        # so is the point: it separates "the source is silent" from "we know the default".
        origin = "unresolved" if extraction.term_id == "unspecified" else "extracted"
        spec.axes[axis_id] = (extraction.term_id, origin, extraction.matched_text)
        if axis_id == "threshold_kind":
            spec.threshold_value = extraction.value_num
            spec.threshold_unit = extraction.unit
            spec.threshold_raw = extraction.matched_text

    if offset is not None:
        spec.timepoint_value = offset.value_num
        spec.timepoint_unit = offset.unit
        spec.timepoint_raw = offset.matched_text
        spec.extractions.append(offset)

    for axis_id in OPERATIONAL_AXES:
        spec.axes.setdefault(axis_id, ("unspecified", "unresolved", None))

    # 5. reviewer override, applied last so it wins over everything derived.
    if override is not None:
        spec.overridden = True
        if override.changes_concept and override.concept_id != spec.concept_id:
            corrected = vocab.concept(override.concept_id)
            spec.concept_id = corrected.concept_id
            # Re-seed the concept's own structure, otherwise the spec would keep the
            # rejected concept's form and measurement under the corrected label.
            for key, term_id in corrected.structure.items():
                axis_id, _role = STRUCTURE_AXES[key]
                spec.axes[axis_id] = (
                    term_id,
                    "human_override",
                    f"reviewer {override.reviewer}: {corrected.concept_id}",
                )
        for axis_id, term_id in override.axes.items():
            spec.axes[axis_id] = (term_id, "human_override", f"reviewer {override.reviewer}")
        # A reviewed spec is asserted, not guessed at, and it is no longer contested.
        spec.match_confidence = 1.0
        spec.ambiguous_tie = False

    spec.unresolved_axes = sorted(
        axis_id
        for axis_id, (term_id, origin, _) in spec.axes.items()
        if term_id == "unspecified" or origin == "unresolved"
    )
    return spec


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def _load_outcomes(conn: duckdb.DuckDBPyConnection, limit: int | None) -> list[OutcomeRecord]:
    sql = """
        SELECT o.outcome_uid, o.study_id, o.endpoint_level,
               coalesce(o.measure, ''), coalesce(o.description, ''), coalesce(o.time_frame, ''),
               coalesce(s.therapeutic_areas, '[]')
        FROM study_outcome o
        LEFT JOIN study s USING (study_id)
        ORDER BY o.study_id, o.endpoint_level, o.ordinal
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    import json as _stdjson

    return [
        OutcomeRecord(
            outcome_uid=row[0],
            study_id=row[1],
            endpoint_level=row[2],
            measure=row[3],
            description=row[4],
            time_frame=row[5],
            therapeutic_areas=tuple(_stdjson.loads(row[6]) or []),
        )
        for row in rows
    ]


def _persist(conn: duckdb.DuckDBPyConnection, spec: EndpointSpec) -> None:
    now = utcnow()
    axes = spec.axes

    def axis_term(axis_id: str) -> str | None:
        entry = axes.get(axis_id)
        return entry[0] if entry else None

    conn.execute("DELETE FROM endpoint_spec WHERE spec_id = ?", [spec.spec_id])
    conn.execute("DELETE FROM endpoint_spec_axis WHERE spec_id = ?", [spec.spec_id])
    conn.execute("DELETE FROM classification_evidence WHERE spec_id = ?", [spec.spec_id])
    conn.execute("DELETE FROM extraction_evidence WHERE spec_id = ?", [spec.spec_id])

    conn.execute(
        """
        INSERT INTO endpoint_spec (
            spec_id, study_id, outcome_uid, concept_id, endpoint_level,
            match_confidence, selected_rule_id, competing_rule_count,
            timepoint_anchor, timepoint_selection, timepoint_value, timepoint_unit, timepoint_raw,
            threshold_kind, threshold_operator, threshold_value, threshold_unit, threshold_raw,
            analysis_population, direction, scale_type, summary_measure,
            unresolved_axes, ambiguous_tie, overridden, derivation_version, classified_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            spec.spec_id,
            spec.outcome.study_id,
            spec.outcome.outcome_uid,
            spec.concept_id,
            spec.outcome.endpoint_level,
            spec.match_confidence,
            spec.selected_rule_id,
            spec.competing_rule_count,
            axis_term("timepoint_anchor"),
            axis_term("timepoint_selection"),
            spec.timepoint_value,
            spec.timepoint_unit,
            spec.timepoint_raw,
            axis_term("threshold_kind"),
            axis_term("threshold_operator"),
            spec.threshold_value,
            spec.threshold_unit,
            spec.threshold_raw,
            axis_term("analysis_population"),
            axis_term("direction"),
            axis_term("scale_type"),
            axis_term("summary_measure"),
            _json(spec.unresolved_axes),
            spec.ambiguous_tie,
            spec.overridden,
            DERIVATION_VERSION,
            now,
        ],
    )

    for axis_id, (term_id, origin, evidence) in sorted(axes.items()):
        conn.execute(
            "INSERT INTO endpoint_spec_axis VALUES (?, ?, ?, ?, ?)",
            [spec.spec_id, axis_id, term_id, origin, evidence],
        )

    for match in spec.rule_matches:
        conn.execute(
            """
            INSERT INTO classification_evidence (
                spec_id, rulepack_id, rulepack_version, rule_id, concept_id,
                priority, confidence, selected, source_field,
                matched_text, span_start, span_end
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                spec.spec_id,
                match.rule.rulepack_id,
                match.rule.rulepack_version,
                match.rule.rule_id,
                match.rule.concept_id,
                match.rule.priority,
                match.rule.confidence,
                match.selected,
                match.source_field,
                match.matched_text,
                match.span_start,
                match.span_end,
            ],
        )

    for extraction in spec.extractions:
        conn.execute(
            """
            INSERT INTO extraction_evidence (
                spec_id, axis_id, extractor_id, extractor_version, source_field,
                matched_text, span_start, span_end, value_text, value_num, unit
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                spec.spec_id,
                extraction.axis_id,
                extraction.extractor_id,
                extraction.extractor_version,
                extraction.source_field,
                extraction.matched_text,
                extraction.span_start,
                extraction.span_end,
                extraction.value_text,
                extraction.value_num,
                extraction.unit,
            ],
        )


def classify_all(
    conn: duckdb.DuckDBPyConnection, limit: int | None = None, vocab: Vocabulary | None = None
) -> dict[str, Any]:
    """Classify every stored outcome, replacing any previous derivation."""
    vocab = vocab or load_vocabulary()
    overrides = active_overrides(conn)
    run_id = uuid.uuid4().hex[:16]
    start_run(conn, run_id, "classify")

    stats = {
        "outcomes": 0,
        "classified": 0,
        "unclassified": 0,
        "overridden": 0,
        "suppressed_by_review": 0,
        "ambiguous_ties": 0,
    }
    try:
        conn.execute("DELETE FROM endpoint_spec")
        conn.execute("DELETE FROM endpoint_spec_axis")
        conn.execute("DELETE FROM classification_evidence")
        conn.execute("DELETE FROM extraction_evidence")
        conn.execute("DELETE FROM unclassified_outcome")

        now = utcnow()
        for outcome in _load_outcomes(conn, limit):
            stats["outcomes"] += 1
            override = overrides.get(outcome.outcome_uid)
            spec = classify_outcome(outcome, vocab, override)
            if spec is None:
                stats["unclassified"] += 1
                if override is not None and override.suppresses:
                    stats["suppressed_by_review"] += 1
                conn.execute(
                    "INSERT INTO unclassified_outcome VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        outcome.outcome_uid,
                        outcome.study_id,
                        outcome.endpoint_level,
                        outcome.measure,
                        (outcome.measure or "").lower().strip(),
                        DERIVATION_VERSION,
                        now,
                    ],
                )
                continue
            _persist(conn, spec)
            stats["classified"] += 1
            stats["overridden"] += int(spec.overridden)
            stats["ambiguous_ties"] += int(spec.ambiguous_tie)

        stats["coverage_pct"] = (
            round(100.0 * stats["classified"] / stats["outcomes"], 1) if stats["outcomes"] else 0.0
        )
        finish_run(conn, run_id, "succeeded", stats)
    except Exception as exc:  # noqa: BLE001
        finish_run(conn, run_id, "failed", stats, error=str(exc))
        raise
    return stats
