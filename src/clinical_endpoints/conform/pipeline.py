"""Orchestrates the conforming pipeline: normalize -> syntactic match ->
semantic fallback -> review queue, over every raw.design_outcomes row, writing
conformed.endpoints (only rows with a resolved measurement) and
conformed.review_queue (everything else -- never auto-conformed at any
confidence, per measurements.yaml's `on_unmatched: review_queue`).
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


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def _row_id(nct_id, outcome_type, measure, time_frame, description) -> str:
    """Stable content hash, not a random uuid -- re-running `conform` on an
    unchanged raw row must produce the same endpoint_id/review_id, the same way
    re-running `pull`/`vocab validate` is a refresh rather than a fresh
    identity each time."""
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
    """Mirrors usdm/envelope.py's own randomisation check on this same
    `raw.studies.allocation` column, so "is this study randomised" is
    answered identically wherever it is asked."""
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
    """docs/EVENT_SEMANTICS_SPEC.md's six conform_row steps: 0) named-endpoint
    match, 1) measurement, 2) reference, 3) form, 4) event, 5) direction.
    Steps 1-3 and threshold parsing are otherwise unchanged from before this
    spec -- a named-endpoint definition only ever FILLS a silent cascade
    (measurement, reference) or WINS outright (form; forms.yaml's own
    resolution still runs, so a disagreement is visible in what
    `resolve_form` would have said, but the definition's form is what ships)."""
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

    # step 0: named-endpoint match. A miss is not a review-queue trigger --
    # every dimension below still runs its own ordinary cascade regardless.
    named_endpoint_hit = resolve.resolve_named_endpoint(rules, fields)
    named_endpoint = (
        rules.named_endpoint_definitions.get(named_endpoint_hit.term_id) if named_endpoint_hit else None
    )
    named_endpoint_id = named_endpoint.id if named_endpoint else None

    # step 1: measurement. The definition's default_measurement fills ONLY
    # when the ordinary cascade (including its semantic fallback) is silent.
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

    # step 2: reference. The definition's reference applies only when the
    # ordinary cascade is silent AND the study is randomised -- asserting
    # "from randomisation" on a single-arm trial would be exactly the
    # unannounced-default disease docs/USDM_PROJECTION_INTEGRITY_SPEC.md
    # exists to cure.
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

    # step 3: form. The definition's form fills only when the ordinary form
    # cascade is silent -- same rule as measurement, and deliberately NOT the
    # unconditional "definition wins" the spec's prose describes: forms.yaml
    # was not asked to migrate any of its own synonyms/patterns (only
    # measurements.yaml's endpoint-name synonyms moved), so its cascade
    # already resolves every named endpoint's typical form correctly on its
    # own -- including "2-Year Overall Survival", which forms.yaml's
    # match_precedence must keep routing to event_free_rate_at_timepoint
    # (a landmark rate), not to `os`'s time_to_event. An unconditional
    # override would silently invert that, which is exactly the kind of
    # regression the zero-churn invariant exists to catch.
    form = resolve.resolve_form(rules, fields, measurement)
    if form.match_method is None and named_endpoint and named_endpoint.form_id:
        form = resolve.FieldMatch(
            named_endpoint.form_id, "named_endpoint", rules.confidence_floor["named_endpoint"], None
        )

    timepoint_result = timepoint.classify(time_frame_raw, rules.timepoint_rules)
    timepoint_result = timepoint.apply_disambiguation(timepoint_result, form.term_id, rules.timepoint_rules)

    # step 4: event. Only for event-family forms (forms.yaml event_family:
    # true) -- everything else carries event_id = NULL, not 'not_stated': a
    # change-from-baseline endpoint does not have an unresolved event, it has
    # no event.
    event_result = None
    if rules.form_event_family.get(form.term_id):
        event_result = resolve.resolve_event(rules, fields, named_endpoint, measurement.term_id)

    # step 5: direction. Event polarity (from a resolved event) first, then
    # the free-text cues, then the measurement's own event_polarity.
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


# conform_row is a pure function of (rules, one row) -- rules is read-only and
# every field on it (compiled regexes, dicts, tuples of frozen dataclasses) is
# picklable, so splitting the row list across worker processes changes
# nothing about the result, only how long it takes to get there. Below this
# row count, ProcessPoolExecutor's own start-up cost (spawning interpreters,
# pickling `rules` once per worker) isn't worth paying.
_MIN_ROWS_FOR_PARALLEL = 200

# Spawn rather than fork: the caller already holds an open DuckDB connection,
# which may have its own background threads, and forking a multi-threaded
# process is a classic way to hand a child a half-locked mutex. Workers here
# never touch `con` -- they only run pure-Python regex/matching code -- but
# spawn sidesteps the question entirely by starting each worker from a clean
# interpreter instead of copying the parent's memory.
_MP_CONTEXT = multiprocessing.get_context("spawn")

# Set once per worker process by _init_worker, so `pool.map` only has to
# pickle one row per task instead of the whole rule set on every call.
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
    """0 (the default) = auto: parallelize across CPUs once there's enough
    work to amortize process start-up, otherwise run serially. A positive
    --jobs always wins, including forcing serial with --jobs 1."""
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
    """Conform every raw.design_outcomes row, wholesale-replacing
    conformed.endpoints and conformed.review_queue (a refresh, like `pull` and
    `vocab validate`, not an append).

    `jobs` controls how many worker processes conform rows in parallel (see
    _resolve_worker_count for the default policy). `on_progress`, if given, is
    called as `on_progress(done, total)` -- once with done=0 before work
    starts (so callers learn `total` even when it's 0), then once per row."""
    if not _table_exists(con, "raw", "design_outcomes"):
        raise ValueError("raw.design_outcomes is empty -- run `endpoints pull` first")
    if not _table_exists(con, "vocab", "matching_cascade"):
        raise ValueError("vocab.matching_cascade is empty -- run `endpoints vocab validate` first")

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
        # A handful of chunks per worker keeps IPC round-trips cheap without
        # making progress updates too bursty.
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
        bulk_insert(
            con, "conformed.endpoints",
            list(ConformedEndpoint.__dataclass_fields__) + ["conformed_at"],
            [(*_astuple(e), now) for e in endpoints],
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
