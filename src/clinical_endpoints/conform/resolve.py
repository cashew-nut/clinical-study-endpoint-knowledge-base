"""Per-dimension cascade resolution: runs matching.yaml's declared cascade
(vocab.matching_cascade) for form/measurement/reference against the fields it
names, honouring forms.yaml's disambiguation block and the reference-implied
form inference on form's third (time_frame) cascade step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from clinical_endpoints.conform import semantic
from clinical_endpoints.conform.rules import ConformRules, NamedEndpointDefinition


@dataclass(frozen=True)
class FieldMatch:
    term_id: str
    # 'exact' | 'syntactic_rule' | 'semantic' | 'named_endpoint' | 'implied' |
    # None (fallback, unmatched)
    match_method: Optional[str]
    confidence: float
    source_field: Optional[str]


def _field_text(fields: dict[str, str], field: str) -> str:
    return fields.get(field) or ""


def resolve_named_endpoint(rules: ConformRules, fields: dict[str, str]) -> Optional[FieldMatch]:
    """docs/EVENT_SEMANTICS_SPEC.md conform_row step 0: measure -> description,
    whole-token/acronym rules exactly as any other dimension. None means no
    named endpoint matched -- not a review-queue trigger, just "step 0 has
    nothing to contribute"."""
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
    """docs/EVENT_SEMANTICS_SPEC.md step 4, in order: (a) the named-endpoint
    definition's own `event`; (b) events.yaml matched directly over
    measure -> description; (c) the resolved measurement's `implies_event`;
    (d) not_stated. Only called for event-family forms -- the caller leaves
    event_id NULL (not 'not_stated') for every other form."""
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
    for step in rules.measurement_cascade.steps:
        text = _field_text(fields, step.field)
        hit = rules.measurement_matcher.match(text)
        if hit:
            return FieldMatch(hit.term_id, step.match_method, rules.confidence_floor[step.match_method], step.field)

    if rules.measurement_cascade.fallback != "review_queue":
        return FieldMatch(rules.measurement_cascade.fallback, None, 0.0, None)

    # Semantic fallback: tried over the same fields the literal cascade read,
    # in the same order, before conceding the row to the review queue.
    for step in rules.measurement_cascade.steps:
        text = _field_text(fields, step.field)
        candidate = semantic.best_match(text, rules.measurement_semantic_index)
        if candidate:
            return FieldMatch(candidate.term_id, "semantic", rules.confidence_floor["semantic"], step.field)
    return None  # genuinely unmatched -> caller diverts this row to review_queue


def _other_disambiguation_members(rule, term_id: str) -> tuple[str, ...]:
    return tuple(m for m in rule.between if m != term_id)


def _disambiguate_form(rules: ConformRules, candidate: FieldMatch, field_text: str, measurement: Optional[FieldMatch]) -> FieldMatch:
    """forms.yaml's disambiguation block. Only overrides when the OTHER member
    of a `between` pair also independently matches the same text -- i.e. the
    wording is genuinely ambiguous, not just "this term happens to be listed in
    a disambiguation rule". The one rule this vocabulary currently declares
    (responder_proportion vs incidence_proportion) is resolved by which member
    has direction_rule `inherit_event_polarity` (an event-polarity form) versus
    `higher_count_better` (a plain count-of-achievers form), per
    vocab/README.md decision #4 -- read from vocab.forms, not hardcoded ids."""
    for rule in rules.form_disambiguation:
        if candidate.term_id not in rule.between:
            continue
        for other_id in _other_disambiguation_members(rule, candidate.term_id):
            if rules.form_matcher.term_matches(other_id, field_text) is None:
                continue  # only one member matches this text -- no real ambiguity

            cand_rule = rules.form_direction_rule.get(candidate.term_id)
            other_rule = rules.form_direction_rule.get(other_id)
            if {cand_rule, other_rule} != {"higher_count_better", "inherit_event_polarity"}:
                continue  # not the event-polarity-decided shape this rule knows how to resolve

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
            # references.yaml: "the conforming pipeline can use [implies_form] to
            # upgrade a form of not_stated, but must record that as an inference,
            # not an exact match." A reference matched from time_frame names its
            # typical form(s) in file order; the first is the default reading
            # (patient_baseline -> change_from_baseline is exactly the documented
            # example for this cascade step).
            text = _field_text(fields, step.field)
            reference_hit = rules.reference_matcher.match(text)
            if reference_hit:
                implied = rules.reference_implies_form.get(reference_hit.term_id)
                if implied:
                    return FieldMatch(implied[0], step.match_method, rules.confidence_floor[step.match_method], step.field)
    return FieldMatch(rules.form_cascade.fallback, None, 0.0, None)
