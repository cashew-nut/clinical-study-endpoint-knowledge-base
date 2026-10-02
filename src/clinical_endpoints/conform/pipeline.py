"""The conforming pipeline over every raw.design_outcomes row: writes
conformed.endpoints (rows with a resolved measurement) and
conformed.review_queue (everything else).

conformed.endpoints.usdm_text is rendered through usdm/project.py, the same
rendering `usdm show` does live, so the parameterised text is queryable by SQL.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import duckdb

from clinical_endpoints.conform import direction as direction_mod
from clinical_endpoints.conform import resolve, semantic, text, threshold, timepoint
from clinical_endpoints.conform.rules import ConformRules, load_rules
from clinical_endpoints.db import bulk_insert
from clinical_endpoints.usdm.project import SourceRow, load_projection_rules, render_endpoint_text


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def _row_id(nct_id, outcome_type, measure, time_frame, description) -> str:
    """Content hash, so re-running `conform` on an unchanged row keeps its id."""
    key = "|".join(x or "" for x in (nct_id, outcome_type, measure, time_frame, description))
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _primary_ta_by_nct(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    if not _table_exists(con, "conformed", "study_therapeutic_area"):
        return {}
    return dict(
        con.execute(
            "SELECT nct_id, ta_id FROM conformed.study_therapeutic_area WHERE is_primary"
        ).fetchall()
    )


def _allocation_by_nct(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    if not _table_exists(con, "raw", "studies"):
        return {}
    return dict(con.execute("SELECT nct_id, allocation FROM raw.studies").fetchall())


def _is_randomised(allocation: str | None) -> bool:
    return (allocation or "").strip().lower().startswith("random")


@dataclass(frozen=True)
class ConformedEndpoint:
    endpoint_id: str
    nct_id: str
    outcome_type: str
    measure_raw: str
    description_raw: str
    time_frame_raw: str
    population: str
    form_id: str
    form_match_method: str
    form_confidence: float
    form_source_field: str
    measurement_id: str
    measurement_match_method: str
    measurement_confidence: float
    measurement_source_field: str
    derivation_id: str | None
    derivation_match_method: str | None
    derivation_confidence: float | None
    derivation_source_field: str | None
    reference_id: str
    reference_match_method: str
    reference_confidence: float
    reference_source_field: str
    event_id: str | None
    event_match_method: str | None
    event_confidence: float | None
    event_source_field: str | None
    named_endpoint_id: str | None
    direction_id: str
    event_polarity_used: str
    scale_id: str
    timepoint_pattern: str
    timepoint_raw: str
    timepoint_match_method: str
    timepoint_extracted: str  # JSON text
    threshold_comparator: str
    threshold_value: float
    threshold_unit: str
    analysable: bool


@dataclass(frozen=True)
class ReviewQueueEntry:
    review_id: str
    nct_id: str
    outcome_type: str
    measure_raw: str
    description_raw: str
    time_frame_raw: str
    population: str
    reason: str
    candidate_form_id: str
    candidate_direction_id: str
    best_semantic_candidate: str
    best_semantic_score: float


def conform_row(
    rules: ConformRules, row: dict, *, ta_id: str | None, allocation: str | None = None
) -> ConformedEndpoint | ReviewQueueEntry:
    """Named-endpoint match, then measurement, derivation, reference, form, event,
    direction. A named-endpoint definition only fills a dimension whose own
    cascade was silent."""
    nct_id, outcome_type = row["nct_id"], row["outcome_type"]
    measure_raw, description_raw, time_frame_raw, population = (
        row.get("measure"), row.get("description"), row.get("time_frame"), row.get("population"),
    )
    row_id = _row_id(nct_id, outcome_type, measure_raw, time_frame_raw, description_raw)

    fields = {
        "measure": text.normalise(measure_raw, rules.normalisation_steps),
        "description": text.normalise(description_raw, rules.normalisation_steps),
        "time_frame": text.normalise(time_frame_raw, rules.normalisation_steps),
    }

    named_endpoint_hit = resolve.resolve_named_endpoint(rules, fields)
    named_endpoint = (
        rules.named_endpoint_definitions.get(named_endpoint_hit.term_id) if named_endpoint_hit else None
    )
    named_endpoint_id = named_endpoint.id if named_endpoint else None

    measurement = resolve.resolve_measurement(rules, fields)
    if measurement is None and named_endpoint and named_endpoint.default_measurement_id:
        measurement = resolve.FieldMatch(
            named_endpoint.default_measurement_id, "named_endpoint",
            rules.confidence_floor["named_endpoint"], None,
        )
    if measurement is None:
        candidate = semantic.best_match(
            fields["measure"] or fields["description"], rules.measurement_semantic_index,
            min_score=0.0, min_overlap=1,
        )
        form_guess = resolve.resolve_form(rules, fields, None)
        direction_guess = direction_mod.derive_direction(
            form_guess.term_id, None, fields["measure"] or fields["description"], rules.direction_rules, ta_id=ta_id
        )
        return ReviewQueueEntry(
            review_id=row_id, nct_id=nct_id, outcome_type=outcome_type,
            measure_raw=measure_raw, description_raw=description_raw, time_frame_raw=time_frame_raw,
            population=population, reason="measurement_unmatched",
            candidate_form_id=form_guess.term_id, candidate_direction_id=direction_guess.direction_id,
            best_semantic_candidate=candidate.term_id if candidate else None,
            best_semantic_score=candidate.score if candidate else None,
        )

    derivation = resolve.resolve_derivation(rules, fields)

    # The definition's reference applies only on a randomised study; asserting
    # "from randomisation" on a single-arm trial would be an unannounced default.
    reference = resolve.resolve_reference(rules, fields)
    if (
        reference.match_method is None
        and named_endpoint
        and named_endpoint.reference_id
        and _is_randomised(allocation)
    ):
        reference = resolve.FieldMatch(
            named_endpoint.reference_id, "named_endpoint", rules.confidence_floor["named_endpoint"], None
        )

    # The definition's form fills only when the form cascade is silent: forms.yaml's
    # own precedence already routes "2-Year Overall Survival" to a landmark rate,
    # and an unconditional override would invert that.
    form = resolve.resolve_form(rules, fields, measurement)
    if form.match_method is None and named_endpoint and named_endpoint.form_id:
        form = resolve.FieldMatch(
            named_endpoint.form_id, "named_endpoint", rules.confidence_floor["named_endpoint"], None
        )

    timepoint_result = timepoint.classify(time_frame_raw, rules.timepoint_rules)
    timepoint_result = timepoint.apply_disambiguation(timepoint_result, form.term_id, rules.timepoint_rules)

    # Event only for event-family forms; other forms carry NULL, not 'not_stated'.
    event_result = None
    if rules.form_event_family.get(form.term_id):
        event_result = resolve.resolve_event(rules, fields, named_endpoint, measurement.term_id)

    cue_text = fields["measure"] or fields["description"]
    direction_result = direction_mod.derive_direction(
        form.term_id, measurement.term_id, cue_text, rules.direction_rules, ta_id=ta_id,
        event_id=event_result.term_id if event_result else None,
    )

    threshold_result = threshold.ThresholdResult(None, None, None)
    if rules.form_expects_threshold.get(form.term_id):
        threshold_result = threshold.parse_threshold(measure_raw)
        if threshold_result.value is None:
            threshold_result = threshold.parse_threshold(description_raw)

    return ConformedEndpoint(
        endpoint_id=row_id, nct_id=nct_id, outcome_type=outcome_type,
        measure_raw=measure_raw, description_raw=description_raw, time_frame_raw=time_frame_raw,
        population=population,
        form_id=form.term_id, form_match_method=form.match_method,
        form_confidence=form.confidence, form_source_field=form.source_field,
        measurement_id=measurement.term_id, measurement_match_method=measurement.match_method,
        measurement_confidence=measurement.confidence, measurement_source_field=measurement.source_field,
        derivation_id=derivation.term_id if derivation else None,
        derivation_match_method=derivation.match_method if derivation else None,
        derivation_confidence=derivation.confidence if derivation else None,
        derivation_source_field=derivation.source_field if derivation else None,
        reference_id=reference.term_id, reference_match_method=reference.match_method,
        reference_confidence=reference.confidence, reference_source_field=reference.source_field,
        event_id=event_result.term_id if event_result else None,
        event_match_method=event_result.match_method if event_result else None,
        event_confidence=event_result.confidence if event_result else None,
        event_source_field=event_result.source_field if event_result else None,
        named_endpoint_id=named_endpoint_id,
        direction_id=direction_result.direction_id, event_polarity_used=direction_result.event_polarity_used,
        scale_id=rules.measurement_default_scale.get(measurement.term_id),
        timepoint_pattern=timepoint_result.pattern_id, timepoint_raw=timepoint_result.raw,
        timepoint_match_method=timepoint_result.match_method,
        timepoint_extracted=json.dumps(timepoint_result.extracted) if timepoint_result.extracted else None,
        threshold_comparator=threshold_result.comparator, threshold_value=threshold_result.value,
        threshold_unit=threshold_result.unit,
        analysable=rules.form_analysable.get(form.term_id, True),
    )


_ENDPOINTS_DDL = """
CREATE OR REPLACE TABLE conformed.endpoints (
    endpoint_id VARCHAR PRIMARY KEY,
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
    usdm_text VARCHAR,
    conformed_at TIMESTAMP
)
"""

_REVIEW_QUEUE_DDL = """
CREATE OR REPLACE TABLE conformed.review_queue (
    review_id VARCHAR PRIMARY KEY,
    nct_id VARCHAR, outcome_type VARCHAR,
    measure_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR, population VARCHAR,
    reason VARCHAR,
    candidate_form_id VARCHAR, candidate_direction_id VARCHAR,
    best_semantic_candidate VARCHAR, best_semantic_score DOUBLE,
    status VARCHAR,
    queued_at TIMESTAMP
)
"""


# Below this row count the process pool's start-up cost outweighs the gain.
_MIN_ROWS_FOR_PARALLEL = 200

# Spawn, not fork: the caller holds an open DuckDB connection with its own threads.
_MP_CONTEXT = multiprocessing.get_context("spawn")

_worker_rules: Optional[ConformRules] = None
_worker_ta_by_nct: dict = {}
_worker_allocation_by_nct: dict = {}


def _init_worker(rules: ConformRules, ta_by_nct: dict, allocation_by_nct: dict) -> None:
    global _worker_rules, _worker_ta_by_nct, _worker_allocation_by_nct
    _worker_rules = rules
    _worker_ta_by_nct = ta_by_nct
    _worker_allocation_by_nct = allocation_by_nct


def _conform_row_worker(row: dict) -> "ConformedEndpoint | ReviewQueueEntry":
    nct_id = row["nct_id"]
    return conform_row(
        _worker_rules, row,
        ta_id=_worker_ta_by_nct.get(nct_id),
        allocation=_worker_allocation_by_nct.get(nct_id),
    )


def _resolve_worker_count(jobs: int, row_count: int) -> int:
    """0 = auto: parallel once there is enough work, else serial. A positive
    `jobs` always wins."""
    if row_count == 0:
        return 1
    if jobs > 0:
        return max(1, min(jobs, row_count))
    if row_count < _MIN_ROWS_FOR_PARALLEL:
        return 1
    return max(1, min(os.cpu_count() or 1, row_count))


def run_conform(
    con: duckdb.DuckDBPyConnection,
    *,
    jobs: int = 0,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Conform every raw.design_outcomes row, replacing conformed.endpoints and
    conformed.review_queue wholesale.

    `on_progress(done, total)` is called once with done=0 before work starts,
    then once per row."""
    if not _table_exists(con, "raw", "design_outcomes"):
        raise ValueError("raw.design_outcomes is empty -- run `endpoints pull` first")
    if not _table_exists(con, "vocab", "matching_cascade"):
        raise ValueError("vocab.matching_cascade is empty -- run `endpoints vocab validate` first")
    if not _table_exists(con, "vocab", "derivations"):
        raise ValueError(
            "vocab.derivations is missing: the warehouse vocabulary predates the derivation "
            "dimension -- run `endpoints vocab validate` first"
        )

    rules = load_rules(con)
    ta_by_nct = _primary_ta_by_nct(con)
    allocation_by_nct = _allocation_by_nct(con)

    rows = con.execute(
        "SELECT nct_id, outcome_type, measure, time_frame, description, population FROM raw.design_outcomes"
    ).fetchall()
    columns = ("nct_id", "outcome_type", "measure", "time_frame", "description", "population")
    row_dicts = [dict(zip(columns, values)) for values in rows]

    endpoints: list[ConformedEndpoint] = []
    review_queue: list[ReviewQueueEntry] = []

    worker_count = _resolve_worker_count(jobs, len(row_dicts))
    if on_progress:
        on_progress(0, len(row_dicts))

    def _collect(i: int, result) -> None:
        (endpoints if isinstance(result, ConformedEndpoint) else review_queue).append(result)
        if on_progress:
            on_progress(i, len(row_dicts))

    if worker_count <= 1:
        for i, row in enumerate(row_dicts, start=1):
            result = conform_row(
                rules, row, ta_id=ta_by_nct.get(row["nct_id"]), allocation=allocation_by_nct.get(row["nct_id"])
            )
            _collect(i, result)
    else:
        chunksize = max(1, len(row_dicts) // (worker_count * 4))
        with ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=_MP_CONTEXT,
            initializer=_init_worker,
            initargs=(rules, ta_by_nct, allocation_by_nct),
        ) as pool:
            for i, result in enumerate(pool.map(_conform_row_worker, row_dicts, chunksize=chunksize), start=1):
                _collect(i, result)

    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(_ENDPOINTS_DDL)
    con.execute(_REVIEW_QUEUE_DDL)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if endpoints:
        projection_rules = load_projection_rules(con)
        usdm_texts = [render_endpoint_text(_source_row(e), projection_rules) for e in endpoints]
        bulk_insert(
            con, "conformed.endpoints",
            list(ConformedEndpoint.__dataclass_fields__) + ["usdm_text", "conformed_at"],
            [(*_astuple(e), usdm_text, now) for e, usdm_text in zip(endpoints, usdm_texts)],
        )
    if review_queue:
        bulk_insert(
            con, "conformed.review_queue",
            list(ReviewQueueEntry.__dataclass_fields__) + ["status", "queued_at"],
            [(*_astuple(e), "pending", now) for e in review_queue],
        )

    return {
        "rows_conformed": len(endpoints),
        "rows_queued": len(review_queue),
        "total_rows": len(rows),
        "workers": worker_count,
    }


def _astuple(obj) -> tuple:
    return tuple(getattr(obj, f) for f in obj.__dataclass_fields__)


def _source_row(e: ConformedEndpoint) -> SourceRow:
    return SourceRow(
        endpoint_id=e.endpoint_id, outcome_type=e.outcome_type,
        measure_raw=e.measure_raw, description_raw=e.description_raw, time_frame_raw=e.time_frame_raw,
        population=e.population, conformed=True,
        form_id=e.form_id, measurement_id=e.measurement_id, derivation_id=e.derivation_id,
        derivation_match_method=e.derivation_match_method, derivation_confidence=e.derivation_confidence,
        reference_id=e.reference_id, event_id=e.event_id, scale_id=e.scale_id, direction_id=e.direction_id,
        timepoint_pattern=e.timepoint_pattern, timepoint_extracted=e.timepoint_extracted,
        threshold_comparator=e.threshold_comparator, threshold_value=e.threshold_value,
        threshold_unit=e.threshold_unit, analysable=e.analysable,
    )
