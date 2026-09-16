"""Per-dimension cascade resolution over the fields matching.yaml names."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from clinical_endpoints.conform import semantic
from clinical_endpoints.conform.rules import ConformRules, NamedEndpointDefinition


@dataclass(frozen=True)
class FieldMatch:
    term_id: str
    # 'exact' | 'syntactic_rule' | 'semantic' | 'named_endpoint' | 'implied' | None (fallback)
    match_method: Optional[str]
    confidence: float
    source_field: Optional[str]


def _field_text(fields: dict[str, str], field: str) -> str:
    return fields.get(field) or ""


def resolve_named_endpoint(rules: ConformRules, fields: dict[str, str]) -> Optional[FieldMatch]:
    for step in rules.named_endpoint_cascade.steps:
        text = _field_text(fields, step.field)
        hit = rules.named_endpoint_matcher.match(text)
        if hit:
            return FieldMatch(hit.term_id, step.match_method, rules.confidence_floor[step.match_method], step.field)
    return None


def resolve_event(
    rules: ConformRules,
    fields: dict[str, str],
    named_endpoint: Optional[NamedEndpointDefinition],
    measurement_id: Optional[str],
) -> FieldMatch:
    """In order: the named-endpoint definition's event, events.yaml matched
    over the cascade fields, the measurement's `implies_event`, not_stated.
    Only called for event-family forms."""
    if named_endpoint is not None and named_endpoint.event_id:
        return FieldMatch(named_endpoint.event_id, "named_endpoint", rules.confidence_floor["named_endpoint"], None)

    for step in rules.event_cascade.steps:
        text = _field_text(fields, step.field)
        hit = rules.event_matcher.match(text)
        if hit:
            return FieldMatch(hit.term_id, step.match_method, rules.confidence_floor[step.match_method], step.field)

    implied = rules.measurement_implies_event.get(measurement_id or "")
    if implied:
        return FieldMatch(implied, "implied", rules.confidence_floor.get("implied", 0.8), None)

    return FieldMatch("not_stated", None, 0.0, None)


def resolve_reference(rules: ConformRules, fields: dict[str, str]) -> FieldMatch:
    for step in rules.reference_cascade.steps:
        text = _field_text(fields, step.field)
        hit = rules.reference_matcher.match(text)
        if hit:
            return FieldMatch(hit.term_id, step.match_method, rules.confidence_floor[step.match_method], step.field)
    return FieldMatch(rules.reference_cascade.fallback, None, 0.0, None)


def resolve_measurement(rules: ConformRules, fields: dict[str, str]) -> Optional[FieldMatch]:
    """None means unmatched; the caller diverts the row to the review queue."""
    for step in rules.measurement_cascade.steps:
        text = _field_text(fields, step.field)
        hit = rules.measurement_matcher.match(text)
        if hit:
            return FieldMatch(hit.term_id, step.match_method, rules.confidence_floor[step.match_method], step.field)

    if rules.measurement_cascade.fallback != "review_queue":
        return FieldMatch(rules.measurement_cascade.fallback, None, 0.0, None)

    for step in rules.measurement_cascade.steps:
        text = _field_text(fields, step.field)
        candidate = semantic.best_match(text, rules.measurement_semantic_index)
        if candidate:
            return FieldMatch(candidate.term_id, "semantic", rules.confidence_floor["semantic"], step.field)
    return None


def _other_disambiguation_members(rule, term_id: str) -> tuple[str, ...]:
    return tuple(m for m in rule.between if m != term_id)


def _disambiguate_form(rules: ConformRules, candidate: FieldMatch, field_text: str, measurement: Optional[FieldMatch]) -> FieldMatch:
    """forms.yaml's disambiguation block. Only overrides when the other member
    of a `between` pair also matches the same text. The one rule declared
    (responder_proportion vs incidence_proportion) is decided by which member
    has direction_rule `inherit_event_polarity` versus `higher_count_better`,
    and whether the measurement is a harm."""
    for rule in rules.form_disambiguation:
        if candidate.term_id not in rule.between:
            continue
        for other_id in _other_disambiguation_members(rule, candidate.term_id):
            if rules.form_matcher.term_matches(other_id, field_text) is None:
                continue

            cand_rule = rules.form_direction_rule.get(candidate.term_id)
            other_rule = rules.form_direction_rule.get(other_id)
            if {cand_rule, other_rule} != {"higher_count_better", "inherit_event_polarity"}:
                continue

            event_polarity_member = candidate.term_id if cand_rule == "inherit_event_polarity" else other_id
            count_member = other_id if event_polarity_member == candidate.term_id else candidate.term_id

            is_harm = False
            if measurement is not None:
                domain = rules.measurement_domain.get(measurement.term_id)
                polarity = rules.direction_rules.measurement_event_polarity.get(measurement.term_id)
                is_harm = polarity == "harm" or domain == "safety"

            winner = event_polarity_member if is_harm else count_member
            if winner != candidate.term_id:
                return FieldMatch(winner, candidate.match_method, candidate.confidence, candidate.source_field)
    return candidate


def resolve_form(rules: ConformRules, fields: dict[str, str], measurement: Optional[FieldMatch]) -> FieldMatch:
    for step in rules.form_cascade.steps:
        if step.field in ("measure", "description"):
            text = _field_text(fields, step.field)
            hit = rules.form_matcher.match(text)
            if hit:
                candidate = FieldMatch(hit.term_id, step.match_method, rules.confidence_floor[step.match_method], step.field)
                return _disambiguate_form(rules, candidate, text, measurement)
        elif step.field == "time_frame":
            # A reference matched from time_frame implies its typical form(s);
            # the first in file order is the default reading.
            text = _field_text(fields, step.field)
            reference_hit = rules.reference_matcher.match(text)
            if reference_hit:
                implied = rules.reference_implies_form.get(reference_hit.term_id)
                if implied:
                    return FieldMatch(implied[0], step.match_method, rules.confidence_floor[step.match_method], step.field)
    return FieldMatch(rules.form_cascade.fallback, None, 0.0, None)
