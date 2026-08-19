"""Per-dimension cascade resolution: runs matching.yaml's declared cascade
(vocab.matching_cascade) for form/measurement/reference against the fields it
names, honouring forms.yaml's disambiguation block and the reference-implied
form inference on form's third (time_frame) cascade step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from clinical_endpoints.conform import semantic
from clinical_endpoints.conform.rules import ConformRules


@dataclass(frozen=True)
class FieldMatch:
    term_id: str
    match_method: Optional[str]  # 'exact' | 'syntactic_rule' | 'semantic' | None (fallback, unmatched)
    confidence: float
    source_field: Optional[str]


def _field_text(fields: dict[str, str], field: str) -> str:
    return fields.get(field) or ""


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
