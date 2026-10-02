"""Conform the results section against the vocabulary and normalise reported
spread into an estimated SD.

* `conformed.endpoint_results`: one row per reported outcome and per baseline
  characteristic, conformed through `conform_row` with the reported title
  standing in for `measure`, and linked to the planned endpoint where one can
  be identified.
* `conformed.endpoint_dispersion`: one row per arm-level measurement, with an
  `sd_estimate` and the path that produced it.
* `conformed.result_group_arm`: each results group linked to the protocol arm
  it reports, and that arm's role (`results/arms.py`).

| `link_method`           | meaning                                                            |
|-------------------------|--------------------------------------------------------------------|
| `exact_title`           | the reported title is, after normalisation, a planned `measure`    |
| `conformed_measurement` | different strings, same conformed measurement in the same study    |
| NULL                    | no planned counterpart; queued in results_review_queue, not joined |
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Optional

import duckdb

from clinical_endpoints.conform.pipeline import (
    ConformedEndpoint,
    conform_row,
)
from clinical_endpoints.conform.rules import load_rules
from clinical_endpoints.conform.text import normalise
from clinical_endpoints.db import bulk_insert
from clinical_endpoints.results import dispersion as dispersion_mod
from clinical_endpoints.results.arms import write_result_group_arm
from clinical_endpoints.results.units import load_unit_rules, resolve_unit, to_si

# The dimension columns shared, name for name, with conformed.endpoints, so
# vocabulary joins work unchanged against either table. `measure_raw` holds the
# reported title.
_SHARED_DIMENSION_COLUMNS: tuple[str, ...] = tuple(
    field for field in ConformedEndpoint.__dataclass_fields__ if field != "endpoint_id"
)

_ENDPOINT_RESULTS_DDL = """
CREATE OR REPLACE TABLE conformed.endpoint_results (
    result_id VARCHAR PRIMARY KEY,
    result_kind VARCHAR,
    source_id VARCHAR,
    nct_id VARCHAR, outcome_type VARCHAR,
    measure_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR, population VARCHAR,
    form_id VARCHAR, form_match_method VARCHAR, form_confidence DOUBLE, form_source_field VARCHAR,
    measurement_id VARCHAR, measurement_match_method VARCHAR, measurement_confidence DOUBLE,
    measurement_source_field VARCHAR,
    derivation_id VARCHAR, derivation_match_method VARCHAR, derivation_confidence DOUBLE, derivation_source_field VARCHAR,
    reference_id VARCHAR, reference_match_method VARCHAR, reference_confidence DOUBLE,
    reference_source_field VARCHAR,
    event_id VARCHAR, event_match_method VARCHAR, event_confidence DOUBLE, event_source_field VARCHAR,
    named_endpoint_id VARCHAR,
    direction_id VARCHAR, event_polarity_used VARCHAR,
    scale_id VARCHAR,
    timepoint_pattern VARCHAR, timepoint_raw VARCHAR, timepoint_match_method VARCHAR,
    timepoint_extracted JSON,
    threshold_comparator VARCHAR, threshold_value DOUBLE, threshold_unit VARCHAR,
    analysable BOOLEAN,
    link_method VARCHAR,
    planned_endpoint_id VARCHAR,
    link_agrees_on_form BOOLEAN,
    conformed_at TIMESTAMP
)
"""

_RESULTS_REVIEW_QUEUE_DDL = """
CREATE OR REPLACE TABLE conformed.results_review_queue (
    review_id VARCHAR PRIMARY KEY,
    result_kind VARCHAR,
    source_id VARCHAR,
    nct_id VARCHAR, outcome_type VARCHAR,
    title_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR,
    reason VARCHAR,
    measurement_id VARCHAR,
    best_semantic_candidate VARCHAR, best_semantic_score DOUBLE,
    status VARCHAR,
    queued_at TIMESTAMP
)
"""

_ENDPOINT_DISPERSION_DDL = """
CREATE OR REPLACE TABLE conformed.endpoint_dispersion (
    dispersion_id VARCHAR PRIMARY KEY,
    result_id VARCHAR, result_kind VARCHAR, source_id VARCHAR, nct_id VARCHAR,
    group_key VARCHAR, group_title VARCHAR,
    class_title VARCHAR, category_title VARCHAR,
    param_type_raw VARCHAR, param_kind VARCHAR,
    dispersion_type_raw VARCHAR, dispersion_kind VARCHAR, confidence_percent DOUBLE,
    unit_raw VARCHAR, scale_id VARCHAR, scale_match_method VARCHAR,
    n INTEGER, n_source VARCHAR,
    central_value DOUBLE,
    sd_estimate DOUBLE, sd_method VARCHAR, sd_is_derived BOOLEAN, sd_is_approximate BOOLEAN,
    sd_scale VARCHAR, sd_skip_reason VARCHAR, sd_inputs JSON,
    sd_estimate_si DOUBLE, si_scale_id VARCHAR,
    computed_at TIMESTAMP
)
"""

_DISPERSION_COLUMNS: tuple[str, ...] = (
    "dispersion_id", "result_id", "result_kind", "source_id", "nct_id",
    "group_key", "group_title", "class_title", "category_title",
    "param_type_raw", "param_kind", "dispersion_type_raw", "dispersion_kind",
    "confidence_percent", "unit_raw", "scale_id", "scale_match_method",
    "n", "n_source", "central_value",
    "sd_estimate", "sd_method", "sd_is_derived", "sd_is_approximate", "sd_scale",
    "sd_skip_reason", "sd_inputs", "sd_estimate_si", "si_scale_id", "computed_at",
)


class NoResults(RuntimeError):
    pass


@dataclass(frozen=True)
class _ResultRow:
    result_kind: str
    source_id: str
    nct_id: str
    outcome_type: Optional[str]
    title: Optional[str]
    description: Optional[str]
    time_frame: Optional[str]
    population: Optional[str]


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def _result_id(result_kind: str, source_id: str) -> str:
    return hashlib.md5(f"{result_kind}|{source_id}".encode("utf-8")).hexdigest()


def _read_result_rows(con: duckdb.DuckDBPyConnection) -> list[_ResultRow]:
    rows: list[_ResultRow] = []
    for source_id, nct_id, outcome_type, title, description, time_frame, population in con.execute(
        """
        SELECT outcome_id, nct_id, outcome_type, title, description, time_frame, population
        FROM raw.outcome_measures
        """
    ).fetchall():
        rows.append(
            _ResultRow("outcome", source_id, nct_id, outcome_type, title, description,
                       time_frame, population)
        )

    if _table_exists(con, "raw", "baseline_measurements"):
        # Characteristic grain: one baseline characteristic conforms once
        # however many arms reported it. time_frame stays NULL rather than
        # "Baseline", which the registry never wrote.
        for source_id, nct_id, title, description, population in con.execute(
            """
            SELECT baseline_id, any_value(nct_id), any_value(title), any_value(description),
                   any_value(population)
            FROM raw.baseline_measurements
            GROUP BY baseline_id
            """
        ).fetchall():
            rows.append(
                _ResultRow("baseline", source_id, nct_id, None, title, description, None, population)
            )
    return rows


def _planned_index(con: duckdb.DuckDBPyConnection, steps: list[str]):
    """Per study: normalised `measure` -> endpoint, and (measurement_id,
    derivation_id) -> endpoint. The derivation is part of the key so a trough FEV1
    result does not link to a planned FEV1 AUC endpoint of the same study."""
    by_title: dict[tuple[str, str], tuple[str, Optional[str]]] = {}
    by_measurement: dict[tuple[str, str, Optional[str]], tuple[str, Optional[str]]] = {}
    if not _table_exists(con, "conformed", "endpoints"):
        return by_title, by_measurement
    for endpoint_id, nct_id, measure_raw, measurement_id, derivation_id, form_id in con.execute(
        "SELECT endpoint_id, nct_id, measure_raw, measurement_id, derivation_id, form_id FROM conformed.endpoints"
    ).fetchall():
        title_key = normalise(measure_raw, steps).lower()
        if title_key:
            by_title.setdefault((nct_id, title_key), (endpoint_id, form_id))
        if measurement_id:
            by_measurement.setdefault((nct_id, measurement_id, derivation_id), (endpoint_id, form_id))
    return by_title, by_measurement


def _unconformed_planned_titles(con: duckdb.DuckDBPyConnection, steps: list[str]):
    """Planned `measure` strings in raw.design_outcomes that did not conform.
    A results row matching one is still an exact title match with no
    conformed endpoint to point at."""
    titles: set[tuple[str, str]] = set()
    if not _table_exists(con, "raw", "design_outcomes"):
        return titles
    for nct_id, measure in con.execute(
        "SELECT nct_id, measure FROM raw.design_outcomes"
    ).fetchall():
        key = normalise(measure, steps).lower()
        if key:
            titles.add((nct_id, key))
    return titles


def run_results_conform(
    con: duckdb.DuckDBPyConnection,
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Conform every results row, link it to the planned endpoint where one
    exists, normalise every arm-level dispersion into an SD estimate, and
    link each results group to its protocol arm. Replaces the four tables
    wholesale."""
    if not _table_exists(con, "raw", "outcome_measures"):
        raise NoResults(
            "raw.outcome_measures is empty -- run `endpoints pull` (without --no-results) first"
        )
    if not _table_exists(con, "vocab", "matching_cascade"):
        raise NoResults("vocab.matching_cascade is empty -- run `endpoints vocab validate` first")

    rules = load_rules(con)
    unit_rules = load_unit_rules(con)
    ta_by_nct = dict(
        con.execute(
            "SELECT nct_id, ta_id FROM conformed.study_therapeutic_area WHERE is_primary"
        ).fetchall()
    ) if _table_exists(con, "conformed", "study_therapeutic_area") else {}
    allocation_by_nct = dict(
        con.execute("SELECT nct_id, allocation FROM raw.studies").fetchall()
    ) if _table_exists(con, "raw", "studies") else {}

    by_title, by_measurement = _planned_index(con, rules.normalisation_steps)
    unconformed_titles = _unconformed_planned_titles(con, rules.normalisation_steps)

    source_rows = _read_result_rows(con)
    if on_progress:
        on_progress(0, len(source_rows))

    conformed_rows: list[tuple] = []
    review_rows: list[tuple] = []
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    measurement_by_result: dict[str, tuple[str, str]] = {}

    for index, row in enumerate(source_rows, start=1):
        result_id = _result_id(row.result_kind, row.source_id)
        outcome = conform_row(
            rules,
            {
                "nct_id": row.nct_id,
                "outcome_type": row.outcome_type or "other",
                "measure": row.title,
                "description": row.description,
                "time_frame": row.time_frame,
                "population": row.population,
            },
            ta_id=ta_by_nct.get(row.nct_id),
            allocation=allocation_by_nct.get(row.nct_id),
        )
        if on_progress:
            on_progress(index, len(source_rows))

        if not isinstance(outcome, ConformedEndpoint):
            review_rows.append(
                (
                    result_id, row.result_kind, row.source_id, row.nct_id, row.outcome_type,
                    row.title, row.description, row.time_frame, "measurement_unmatched", None,
                    outcome.best_semantic_candidate, outcome.best_semantic_score, "pending", now,
                )
            )
            continue

        title_key = normalise(row.title, rules.normalisation_steps).lower()
        link_method: Optional[str] = None
        planned_endpoint_id: Optional[str] = None
        planned_form_id: Optional[str] = None
        if row.result_kind == "outcome":
            planned = by_title.get((row.nct_id, title_key)) if title_key else None
            if planned is not None:
                link_method = "exact_title"
                planned_endpoint_id, planned_form_id = planned
            elif title_key and (row.nct_id, title_key) in unconformed_titles:
                link_method = "exact_title"
            else:
                planned = by_measurement.get((row.nct_id, outcome.measurement_id, outcome.derivation_id))
                if planned is not None:
                    link_method = "conformed_measurement"
                    planned_endpoint_id, planned_form_id = planned

        conformed_rows.append(
            (
                result_id, row.result_kind, row.source_id,
                *(getattr(outcome, field) for field in _SHARED_DIMENSION_COLUMNS),
                link_method, planned_endpoint_id,
                None if planned_form_id is None else planned_form_id == outcome.form_id,
                now,
            )
        )
        measurement_by_result[result_id] = (outcome.measurement_id, outcome.form_id)

        # Baseline characteristics are unlinked by construction and never queued for it.
        if row.result_kind == "outcome" and link_method is None:
            review_rows.append(
                (
                    result_id, row.result_kind, row.source_id, row.nct_id, row.outcome_type,
                    row.title, row.description, row.time_frame, "unlinked_to_planned",
                    outcome.measurement_id, None, None, "pending", now,
                )
            )

    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(_ENDPOINT_RESULTS_DDL)
    con.execute(_RESULTS_REVIEW_QUEUE_DDL)
    if conformed_rows:
        bulk_insert(
            con,
            "conformed.endpoint_results",
            [
                "result_id", "result_kind", "source_id", *_SHARED_DIMENSION_COLUMNS,
                "link_method", "planned_endpoint_id", "link_agrees_on_form", "conformed_at",
            ],
            conformed_rows,
        )
    if review_rows:
        bulk_insert(
            con,
            "conformed.results_review_queue",
            [
                "review_id", "result_kind", "source_id", "nct_id", "outcome_type", "title_raw",
                "description_raw", "time_frame_raw", "reason", "measurement_id",
                "best_semantic_candidate", "best_semantic_score", "status", "queued_at",
            ],
            review_rows,
        )

    dispersion_counts = _write_dispersion(con, unit_rules, now)
    arm_counts = write_result_group_arm(con)

    return {
        "results_rows": len(source_rows),
        "rows_conformed": len(conformed_rows),
        "rows_queued": len(review_rows),
        "links": dict(
            con.execute(
                """
                SELECT coalesce(link_method, 'unlinked'), count(*)
                FROM conformed.endpoint_results WHERE result_kind = 'outcome' GROUP BY 1
                """
            ).fetchall()
        ),
        **dispersion_counts,
        "arm_links": arm_counts["links"],
    }


def _write_dispersion(
    con: duckdb.DuckDBPyConnection, unit_rules, now: dt.datetime
) -> dict:
    """One row per arm-level measurement, including the ones no SD could be
    derived from; those are the denominator."""
    con.execute(_ENDPOINT_DISPERSION_DDL)

    # Arm n from the measurement row when the source carried one, else from
    # the outcome's arm denominator.
    outcome_rows = con.execute(
        """
        SELECT
            m.outcome_id, m.nct_id, m.group_key, g.title,
            m.class_title, m.category_title,
            o.param_type, o.dispersion_type, o.unit_of_measure,
            m.param_value_num, m.dispersion_value_num,
            m.dispersion_lower_limit, m.dispersion_upper_limit,
            coalesce(m.n, g.n) AS n,
            CASE WHEN m.n IS NOT NULL THEN 'measurement'
                 WHEN g.n IS NOT NULL THEN 'outcome_group' END AS n_source
        FROM raw.outcome_measurements m
        JOIN raw.outcome_measures o USING (outcome_id)
        LEFT JOIN raw.outcome_groups g
               ON g.outcome_id = m.outcome_id AND g.group_key = m.group_key
        """
    ).fetchall()

    baseline_rows = []
    if _table_exists(con, "raw", "baseline_measurements"):
        baseline_rows = con.execute(
            """
            SELECT baseline_id, nct_id, group_key, group_title, class_title, category_title,
                   param_type, dispersion_type, unit_of_measure,
                   param_value_num, dispersion_value_num,
                   dispersion_lower_limit, dispersion_upper_limit,
                   n, CASE WHEN n IS NOT NULL THEN 'baseline_row' END
            FROM raw.baseline_measurements
            """
        ).fetchall()

    rows: list[tuple] = []
    for result_kind, source in (("outcome", outcome_rows), ("baseline", baseline_rows)):
        for (
            source_id, nct_id, group_key, group_title, class_title, category_title,
            param_type, dispersion_type, unit_raw,
            central_value, dispersion_value, lower_limit, upper_limit, n, n_source,
        ) in source:
            estimate = dispersion_mod.estimate_sd(
                param_type=param_type,
                dispersion_type=dispersion_type,
                dispersion_value=dispersion_value,
                lower_limit=lower_limit,
                upper_limit=upper_limit,
                n=n,
            )
            unit = resolve_unit(unit_raw, unit_rules)
            sd_si, si_scale_id = to_si(unit.scale_id, estimate.value, unit_rules)
            # A log-scale SD is not convertible by a unit factor.
            if estimate.scale != "arithmetic":
                sd_si, si_scale_id = None, None
            result_id = _result_id(result_kind, source_id)
            rows.append(
                (
                    hashlib.md5(
                        "|".join(
                            [
                                result_id, group_key or "", class_title or "", category_title or "",
                            ]
                        ).encode("utf-8")
                    ).hexdigest(),
                    result_id, result_kind, source_id, nct_id,
                    group_key, group_title, class_title, category_title,
                    param_type, estimate.inputs.get("param_kind"),
                    dispersion_type, estimate.inputs.get("dispersion_kind"),
                    dispersion_mod.confidence_percent(dispersion_type),
                    unit_raw, unit.scale_id, unit.match_method,
                    n, n_source, central_value,
                    estimate.value, estimate.method, estimate.is_derived, estimate.is_approximate,
                    estimate.scale, estimate.skip_reason, json.dumps(estimate.inputs),
                    sd_si, si_scale_id, now,
                )
            )

    if rows:
        # Keep the last row per id, as `upsert_rows` does.
        deduped = {row[0]: row for row in rows}
        bulk_insert(
            con, "conformed.endpoint_dispersion", list(_DISPERSION_COLUMNS), list(deduped.values())
        )

    usable = con.execute(
        "SELECT count(*) FROM conformed.endpoint_dispersion WHERE sd_estimate IS NOT NULL"
    ).fetchone()[0]
    return {
        "dispersion_rows": len(rows),
        "dispersion_with_sd": usable,
        "sd_methods": dict(
            con.execute(
                """
                SELECT sd_method, count(*) FROM conformed.endpoint_dispersion
                WHERE sd_estimate IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
                """
            ).fetchall()
        ),
    }
