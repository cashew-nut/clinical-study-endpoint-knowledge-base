"""Load every vocab.* table the conforming pipeline needs, once per `conform`
run -- mirrors ta/resolver.py's `TaMapping` / `load_ta_mapping`."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from clinical_endpoints.conform import matcher, semantic, timepoint
from clinical_endpoints.conform.cascade import Cascade, load_cascade, load_confidence_floor
from clinical_endpoints.conform.direction import DirectionRules, load_direction_rules
from clinical_endpoints.conform.matcher import MatchSettings, TermMatcher

# dimension -> the vocab.* table holding its scalar columns (DimensionSpec.table).
_DIMENSION_TABLE = {
    "form": "forms",
    "measurement": "measurements",
    "reference": "references",
}


@dataclass(frozen=True)
class FormDisambiguationRule:
    between: tuple[str, ...]
    prefer: str
    otherwise: str


@dataclass(frozen=True)
class ConformRules:
    normalisation_steps: list[str]
    match_settings: MatchSettings
    confidence_floor: dict[str, float]

    form_matcher: TermMatcher
    measurement_matcher: TermMatcher
    reference_matcher: TermMatcher

    form_cascade: Cascade
    measurement_cascade: Cascade
    reference_cascade: Cascade
    timepoint_cascade: Cascade

    form_analysable: dict[str, bool]
    form_expects_threshold: dict[str, bool]
    form_direction_rule: dict[str, str]
    form_disambiguation: tuple[FormDisambiguationRule, ...]

    measurement_default_scale: dict[str, str]
    measurement_domain: dict[str, str]

    reference_implies_form: dict[str, tuple[str, ...]]  # reference_id -> form ids, in file order
    reference_kind: dict[str, str]

    timepoint_rules: timepoint.TimepointRules
    direction_rules: DirectionRules

    measurement_semantic_index: tuple

    def dimension_table(self, dimension: str) -> str:
        return _DIMENSION_TABLE[dimension]


def load_rules(con: duckdb.DuckDBPyConnection) -> ConformRules:
    from clinical_endpoints.conform.text import load_normalisation_steps

    normalisation_steps = load_normalisation_steps(con)
    match_settings = matcher.load_settings(con)
    confidence_floor = load_confidence_floor(con)

    form_matcher = matcher.build_matcher(con, "form", "forms", match_settings)
    measurement_matcher = matcher.build_matcher(con, "measurement", "measurements", match_settings)
    reference_matcher = matcher.build_matcher(con, "reference", "references", match_settings)

    form_analysable = {
        row[0]: (row[1] == "true") for row in con.execute("SELECT id, analysable FROM vocab.forms").fetchall()
    }
    form_expects_threshold = {
        row[0]: (row[1] == "true") for row in con.execute("SELECT id, expects_threshold FROM vocab.forms").fetchall()
    }
    form_direction_rule = dict(con.execute("SELECT id, direction_rule FROM vocab.forms").fetchall())

    form_disambiguation = tuple(
        FormDisambiguationRule(tuple(between), prefer, otherwise)
        for _ordinal, between, prefer, otherwise in con.execute(
            "SELECT ordinal, between_forms, prefer, otherwise FROM vocab.form_disambiguation ORDER BY ordinal"
        ).fetchall()
    )

    measurement_default_scale = {
        row[0]: row[1]
        for row in con.execute("SELECT id, default_scale FROM vocab.measurements").fetchall()
        if row[1] is not None
    }
    measurement_domain = dict(con.execute("SELECT id, domain FROM vocab.measurements").fetchall())

    reference_implies_form: dict[str, list[str]] = {}
    for reference_id, form_id in con.execute("SELECT reference_id, form_id FROM vocab.reference_implies_form").fetchall():
        reference_implies_form.setdefault(reference_id, []).append(form_id)
    reference_kind = dict(con.execute("SELECT id, kind FROM vocab.references").fetchall())

    return ConformRules(
        normalisation_steps=normalisation_steps,
        match_settings=match_settings,
        confidence_floor=confidence_floor,
        form_matcher=form_matcher,
        measurement_matcher=measurement_matcher,
        reference_matcher=reference_matcher,
        form_cascade=load_cascade(con, "form"),
        measurement_cascade=load_cascade(con, "measurement"),
        reference_cascade=load_cascade(con, "reference"),
        timepoint_cascade=load_cascade(con, "timepoint"),
        form_analysable=form_analysable,
        form_expects_threshold=form_expects_threshold,
        form_direction_rule=form_direction_rule,
        form_disambiguation=form_disambiguation,
        measurement_default_scale=measurement_default_scale,
        measurement_domain=measurement_domain,
        reference_implies_form={k: tuple(v) for k, v in reference_implies_form.items()},
        reference_kind=reference_kind,
        timepoint_rules=timepoint.load_timepoint_rules(con),
        direction_rules=load_direction_rules(con),
        measurement_semantic_index=semantic.build_semantic_index(con, "measurement", "measurements"),
    )
