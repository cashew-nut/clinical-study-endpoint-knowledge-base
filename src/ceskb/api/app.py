"""HTTP API behind the exploration UI.

Read-only by default: every endpoint here opens the DuckDB file read-only, so the UI
cannot mutate the knowledge base and can be pointed at a shared copy safely.

The single exception is recording a reviewer decision, which is off unless the server
was started with `ceskb serve --allow-review`. It is opt-in rather than always-on
because it writes a git-tracked file and re-derives Layer B, and because a queue you
cannot act on is only half a review loop -- so the capability exists, but nobody gets
it by accident.
"""

from __future__ import annotations

import json
import os
from typing import Any

import duckdb
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ceskb.config import DERIVATION_VERSION, PATHS
from ceskb.project.usdm import USDM_VERSION

app = FastAPI(title="Clinical Study Endpoint Knowledge Base", version="0.1.0")

#: Read from the environment rather than passed in, because uvicorn loads this module
#: by import string and never sees the CLI's arguments.
ALLOW_REVIEW_ENV = "CESKB_ALLOW_REVIEW"


def _review_writes_allowed() -> bool:
    return os.environ.get(ALLOW_REVIEW_ENV, "").lower() in {"1", "true", "yes"}


def _conn() -> duckdb.DuckDBPyConnection:
    if not PATHS.database.exists():
        raise HTTPException(
            status_code=503,
            detail="database not built; run `ceskb refresh --source fixtures`",
        )
    return duckdb.connect(str(PATHS.database), read_only=True)


def _rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    with _conn() as conn:
        totals = conn.execute(
            """
            SELECT (SELECT count(*) FROM concept)          AS concepts,
                   (SELECT count(*) FROM axis)             AS axes,
                   (SELECT count(*) FROM term)             AS terms,
                   (SELECT count(*) FROM rule)             AS rules,
                   (SELECT count(*) FROM study)            AS studies,
                   (SELECT count(*) FROM study_outcome)    AS outcomes,
                   (SELECT count(*) FROM endpoint_spec)    AS specs,
                   (SELECT count(*) FROM unclassified_outcome) AS unclassified,
                   (SELECT count(*) FROM usdm_projection)  AS projections
            """
        )
        counts = _rows(totals)[0]
        coverage = _rows(conn.execute("SELECT * FROM coverage_summary ORDER BY 1"))
        synthetic = conn.execute(
            "SELECT count(*) FROM study WHERE conditions LIKE '%__SYNTHETIC_FIXTURE__%'"
        ).fetchone()[0]
        last_runs = _rows(
            conn.execute(
                "SELECT run_id, stage, status, started_at, finished_at, stats "
                "FROM pipeline_run ORDER BY started_at DESC LIMIT 6"
            )
        )
    for run in last_runs:
        run["stats"] = _loads(run.get("stats"), {})
    total_outcomes = counts["outcomes"] or 0
    return {
        "counts": counts,
        "coverage_by_level": coverage,
        "coverage_pct": round(100.0 * counts["specs"] / total_outcomes, 1) if total_outcomes else 0.0,
        "synthetic_studies": synthetic,
        "derivation_version": DERIVATION_VERSION,
        "usdm_version": USDM_VERSION,
    } | {"recent_runs": last_runs}


@app.get("/api/axes")
def axes() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = _rows(
            conn.execute(
                """
                SELECT a.axis_id, a.label, a.definition, a.extensible,
                       a.usdm_entity, a.usdm_attribute, a.usdm_codelist_c_code,
                       count(t.term_id) AS term_count
                FROM axis a LEFT JOIN term t USING (axis_id)
                GROUP BY ALL ORDER BY a.axis_id
                """
            )
        )
    return rows


@app.get("/api/axes/{axis_id}")
def axis_detail(axis_id: str) -> dict[str, Any]:
    with _conn() as conn:
        axis = _rows(conn.execute("SELECT * FROM axis WHERE axis_id = ?", [axis_id]))
        if not axis:
            raise HTTPException(404, f"unknown axis '{axis_id}'")
        terms = _rows(
            conn.execute(
                """
                SELECT t.term_id, t.label, t.definition, t.synonyms, t.broader,
                       t.attributes, t.status,
                       coalesce(p.spec_count, 0)  AS spec_count,
                       coalesce(p.study_count, 0) AS study_count
                FROM term t
                LEFT JOIN axis_term_prevalence p
                       ON p.axis_id = t.axis_id AND p.term_id = t.term_id
                WHERE t.axis_id = ?
                ORDER BY coalesce(p.spec_count, 0) DESC, t.term_id
                """,
                [axis_id],
            )
        )
        mappings = _rows(
            conn.execute(
                "SELECT term_id, system, code, display, verified, verified_against "
                "FROM term_external_mapping WHERE axis_id = ?",
                [axis_id],
            )
        )
    by_term: dict[str, list[dict[str, Any]]] = {}
    for mapping in mappings:
        by_term.setdefault(mapping["term_id"], []).append(mapping)
    for term in terms:
        term["synonyms"] = _loads(term.get("synonyms"), [])
        term["attributes"] = _loads(term.get("attributes"), {})
        term["external_mappings"] = by_term.get(term["term_id"], [])
    return {"axis": axis[0], "terms": terms}


@app.get("/api/concepts")
def concepts(
    q: str | None = None,
    therapeutic_area: str | None = None,
    endpoint_form: str | None = None,
    only_observed: bool = False,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if q:
        clauses.append("(lower(c.label) LIKE ? OR lower(c.concept_id) LIKE ? OR lower(c.synonyms) LIKE ?)")
        needle = f"%{q.lower()}%"
        params += [needle, needle, needle]
    if therapeutic_area:
        clauses.append(
            "EXISTS (SELECT 1 FROM concept_therapeutic_area ta "
            "WHERE ta.concept_id = c.concept_id AND ta.therapeutic_area = ?)"
        )
        params.append(therapeutic_area)
    if endpoint_form:
        clauses.append(
            "EXISTS (SELECT 1 FROM concept_structure cs WHERE cs.concept_id = c.concept_id "
            "AND cs.axis_id = 'endpoint_form' AND cs.term_id = ?)"
        )
        params.append(endpoint_form)
    if only_observed:
        clauses.append("coalesce(p.spec_count, 0) > 0")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as conn:
        rows = _rows(
            conn.execute(
                f"""
                SELECT c.concept_id, c.label, c.abbreviation, c.definition,
                       c.therapeutic_areas, c.status,
                       coalesce(p.spec_count, 0)    AS spec_count,
                       coalesce(p.study_count, 0)   AS study_count,
                       coalesce(p.primary_count, 0) AS primary_count,
                       (SELECT term_id FROM concept_structure s
                         WHERE s.concept_id = c.concept_id AND s.axis_id = 'endpoint_form')
                           AS endpoint_form,
                       (SELECT term_id FROM concept_structure s
                         WHERE s.concept_id = c.concept_id AND s.axis_id = 'measurement_concept')
                           AS measurement_concept
                FROM concept c
                LEFT JOIN concept_prevalence p USING (concept_id)
                {where}
                ORDER BY coalesce(p.spec_count, 0) DESC, c.concept_id
                """,
                params,
            )
        )
    for row in rows:
        row["therapeutic_areas"] = _loads(row.get("therapeutic_areas"), [])
    return rows


@app.get("/api/concepts/{concept_id}")
def concept_detail(concept_id: str) -> dict[str, Any]:
    with _conn() as conn:
        concept = _rows(conn.execute("SELECT * FROM concept WHERE concept_id = ?", [concept_id]))
        if not concept:
            raise HTTPException(404, f"unknown concept '{concept_id}'")
        record = concept[0]
        record["synonyms"] = _loads(record.get("synonyms"), [])
        record["therapeutic_areas"] = _loads(record.get("therapeutic_areas"), [])
        record["usdm_hints"] = _loads(record.get("usdm_hints"), {})

        structure = _rows(
            conn.execute(
                """
                SELECT s.axis_id, s.term_id, s.role, t.label AS term_label,
                       t.definition AS term_definition, a.label AS axis_label
                FROM concept_structure s
                LEFT JOIN term t ON t.axis_id = s.axis_id AND t.term_id = s.term_id
                LEFT JOIN axis a ON a.axis_id = s.axis_id
                WHERE s.concept_id = ? ORDER BY s.role DESC, s.axis_id
                """,
                [concept_id],
            )
        )
        threshold = _rows(
            conn.execute("SELECT * FROM concept_threshold WHERE concept_id = ?", [concept_id])
        )
        components = _rows(
            conn.execute(
                "SELECT ordinal, label, component_concept_id, measurement_concept, note "
                "FROM concept_component WHERE concept_id = ? ORDER BY ordinal",
                [concept_id],
            )
        )
        relations = _rows(
            conn.execute(
                """
                SELECT r.related_concept_id, r.relation, r.note, c.label
                FROM concept_relation r LEFT JOIN concept c ON c.concept_id = r.related_concept_id
                WHERE r.concept_id = ?
                UNION ALL
                SELECT r.concept_id, 'inverse:' || r.relation, r.note, c.label
                FROM concept_relation r LEFT JOIN concept c ON c.concept_id = r.concept_id
                WHERE r.related_concept_id = ?
                """,
                [concept_id, concept_id],
            )
        )
        criteria = _rows(
            conn.execute(
                "SELECT name, version, citation, url FROM concept_criteria WHERE concept_id = ?",
                [concept_id],
            )
        )
        rules = _rows(
            conn.execute(
                "SELECT rulepack_id, rulepack_version, rule_id, priority, confidence, "
                "match_spec, asserts, notes FROM rule WHERE concept_id = ? ORDER BY priority DESC",
                [concept_id],
            )
        )
        for rule in rules:
            rule["match_spec"] = _loads(rule.get("match_spec"), {})
            rule["asserts"] = _loads(rule.get("asserts"), {})

        evidence = _rows(
            conn.execute(
                """
                SELECT e.spec_id, e.study_id, e.endpoint_level, e.match_confidence,
                       e.selected_rule_id, e.timepoint_anchor, e.timepoint_selection,
                       e.timepoint_value, e.timepoint_unit, e.timepoint_raw,
                       e.threshold_value, e.threshold_unit, e.analysis_population,
                       o.measure, o.time_frame,
                       s.brief_title, s.lead_sponsor, s.phases,
                       s.conditions LIKE '%__SYNTHETIC_FIXTURE__%' AS is_synthetic
                FROM endpoint_spec e
                JOIN study_outcome o USING (outcome_uid)
                LEFT JOIN study s ON s.study_id = e.study_id
                WHERE e.concept_id = ?
                ORDER BY CASE e.endpoint_level WHEN 'primary' THEN 0
                                               WHEN 'secondary' THEN 1 ELSE 2 END,
                         e.study_id
                """,
                [concept_id],
            )
        )
        for row in evidence:
            row["phases"] = _loads(row.get("phases"), [])

        # Which parameter values this concept actually takes in the wild, and how often.
        observed = _rows(
            conn.execute(
                """
                SELECT a.axis_id, a.term_id, a.origin, count(*) AS n
                FROM endpoint_spec_axis a
                JOIN endpoint_spec e USING (spec_id)
                WHERE e.concept_id = ?
                GROUP BY ALL ORDER BY a.axis_id, n DESC
                """,
                [concept_id],
            )
        )
    return {
        "concept": record,
        "structure": structure,
        "definitional_threshold": threshold[0] if threshold else None,
        "components": components,
        "relations": relations,
        "governing_criteria": criteria,
        "rules": rules,
        "evidence": evidence,
        "observed_parameters": observed,
    }


@app.get("/api/specs/{spec_id}")
def spec_detail(spec_id: str) -> dict[str, Any]:
    """The full provenance trace for one endpoint specification."""
    with _conn() as conn:
        spec = _rows(
            conn.execute(
                """
                SELECT e.*, o.measure, o.description, o.time_frame,
                       s.brief_title, s.official_title, s.lead_sponsor, s.conditions,
                       s.conditions LIKE '%__SYNTHETIC_FIXTURE__%' AS is_synthetic
                FROM endpoint_spec e
                JOIN study_outcome o USING (outcome_uid)
                LEFT JOIN study s ON s.study_id = e.study_id
                WHERE e.spec_id = ?
                """,
                [spec_id],
            )
        )
        if not spec:
            raise HTTPException(404, f"unknown spec '{spec_id}'")
        record = spec[0]
        record["conditions"] = _loads(record.get("conditions"), [])
        record["unresolved_axes"] = _loads(record.get("unresolved_axes"), [])

        axes_rows = _rows(
            conn.execute(
                """
                SELECT a.axis_id, a.term_id, a.origin, a.evidence,
                       t.label AS term_label, ax.label AS axis_label
                FROM endpoint_spec_axis a
                LEFT JOIN term t ON t.axis_id = a.axis_id AND t.term_id = a.term_id
                LEFT JOIN axis ax ON ax.axis_id = a.axis_id
                WHERE a.spec_id = ? ORDER BY a.axis_id
                """,
                [spec_id],
            )
        )
        rules = _rows(
            conn.execute(
                "SELECT * FROM classification_evidence WHERE spec_id = ? "
                "ORDER BY selected DESC, priority DESC",
                [spec_id],
            )
        )
        extractions = _rows(
            conn.execute(
                "SELECT * FROM extraction_evidence WHERE spec_id = ? ORDER BY axis_id",
                [spec_id],
            )
        )
    return {
        "spec": record,
        "axes": axes_rows,
        "rule_evidence": rules,
        "extraction_evidence": extractions,
    }


@app.get("/api/studies")
def studies(q: str | None = None, limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    clause, params = "", []
    if q:
        clause = "WHERE lower(s.brief_title) LIKE ? OR lower(s.study_id) LIKE ? OR lower(s.conditions) LIKE ?"
        needle = f"%{q.lower()}%"
        params = [needle, needle, needle]
    with _conn() as conn:
        rows = _rows(
            conn.execute(
                f"""
                SELECT s.study_id, s.brief_title, s.lead_sponsor, s.overall_status,
                       s.phases, s.conditions, s.therapeutic_areas, s.last_update_posted,
                       s.conditions LIKE '%__SYNTHETIC_FIXTURE__%' AS is_synthetic,
                       count(e.spec_id) AS spec_count
                FROM study s LEFT JOIN endpoint_spec e USING (study_id)
                {clause}
                GROUP BY ALL ORDER BY s.study_id LIMIT {int(limit)}
                """,
                params,
            )
        )
    for row in rows:
        for key in ("phases", "conditions", "therapeutic_areas"):
            row[key] = _loads(row.get(key), [])
    return rows


@app.get("/api/studies/{study_id}")
def study_detail(study_id: str) -> dict[str, Any]:
    with _conn() as conn:
        study = _rows(conn.execute("SELECT * FROM study WHERE study_id = ?", [study_id]))
        if not study:
            raise HTTPException(404, f"unknown study '{study_id}'")
        record = study[0]
        for key in ("phases", "conditions", "therapeutic_areas"):
            record[key] = _loads(record.get(key), [])
        record["is_synthetic"] = "__SYNTHETIC_FIXTURE__" in record["conditions"]

        outcomes = _rows(
            conn.execute(
                """
                SELECT o.outcome_uid, o.endpoint_level, o.ordinal, o.measure, o.time_frame,
                       e.spec_id, e.concept_id, e.match_confidence, c.label AS concept_label
                FROM study_outcome o
                LEFT JOIN endpoint_spec e USING (outcome_uid)
                LEFT JOIN concept c ON c.concept_id = e.concept_id
                WHERE o.study_id = ?
                ORDER BY CASE o.endpoint_level WHEN 'primary' THEN 0
                                               WHEN 'secondary' THEN 1 ELSE 2 END, o.ordinal
                """,
                [study_id],
            )
        )
        has_usdm = conn.execute(
            "SELECT count(*) FROM usdm_projection WHERE study_id = ?", [study_id]
        ).fetchone()[0]
    return {"study": record, "outcomes": outcomes, "has_usdm": bool(has_usdm)}


@app.get("/api/usdm/{study_id}")
def usdm(study_id: str) -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT document FROM usdm_projection WHERE study_id = ?", [study_id]
        ).fetchone()
    if not row:
        raise HTTPException(404, f"no USDM projection for '{study_id}'")
    return json.loads(row[0])


@app.get("/api/unclassified")
def unclassified(limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    """Unmatched outcome text, most frequent first: the queue for new rules."""
    with _conn() as conn:
        return _rows(
            conn.execute(
                """
                SELECT normalised_measure, count(*) AS n,
                       min(study_id) AS example_study, min(endpoint_level) AS level
                FROM unclassified_outcome
                GROUP BY normalised_measure ORDER BY n DESC, normalised_measure
                LIMIT ?
                """,
                [limit],
            )
        )


@app.get("/api/prevalence/axes")
def axis_prevalence() -> list[dict[str, Any]]:
    with _conn() as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM axis_term_prevalence ORDER BY axis_id, spec_count DESC"
            )
        )


# --------------------------------------------------------------------------- #
# review and evaluation
# --------------------------------------------------------------------------- #
@app.get("/api/review/queue")
def review_queue(
    reason: str | None = None, limit: int = Query(100, le=1000)
) -> dict[str, Any]:
    """Specifications a human should look at, most doubtful first."""
    clause, params = "", []
    if reason:
        clause = "WHERE reason = ?"
        params = [reason]
    with _conn() as conn:
        queue = _rows(
            conn.execute(
                f"SELECT * FROM review_queue {clause} "
                f"ORDER BY review_score DESC, outcome_uid LIMIT {int(limit)}",
                params,
            )
        )
        by_reason = _rows(
            conn.execute(
                "SELECT reason, count(*) AS n FROM review_queue GROUP BY reason ORDER BY n DESC"
            )
        )
        decided = _rows(
            conn.execute(
                "SELECT status, count(*) AS n FROM spec_override GROUP BY status"
            )
        )
    return {"queue": queue, "by_reason": by_reason, "decisions": decided}


@app.get("/api/review/overrides")
def overrides() -> list[dict[str, Any]]:
    """Every reviewer decision on record, including the stale and retired ones."""
    with _conn() as conn:
        rows = _rows(
            conn.execute(
                """
                SELECT ov.*, o.measure, e.spec_id
                FROM spec_override ov
                LEFT JOIN study_outcome o USING (outcome_uid)
                LEFT JOIN endpoint_spec e USING (outcome_uid)
                ORDER BY ov.decided_at DESC
                """
            )
        )
    for row in rows:
        row["axes"] = _loads(row.get("axes"), {})
    return rows


@app.get("/api/review/evaluations")
def evaluations(limit: int = Query(20, le=200)) -> list[dict[str, Any]]:
    """Scored runs, newest first, so accuracy reads as a series rather than a claim."""
    with _conn() as conn:
        rows = _rows(
            conn.execute(
                """
                SELECT evaluation_id, gold_set_id, gold_set_version, independence,
                       derivation_version, evaluated_at, items_total, items_scored,
                       items_stale, concept_accuracy, macro_precision, macro_recall,
                       macro_f1, report
                FROM evaluation_run ORDER BY evaluated_at DESC LIMIT ?
                """,
                [limit],
            )
        )
    for row in rows:
        row["report"] = _loads(row.get("report"), {})
    return rows


@app.post("/api/review/overrides")
def record_review_decision(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Record a reviewer decision, then re-derive so it takes effect immediately.

    Disabled unless the server was started with --allow-review.
    """
    if not _review_writes_allowed():
        raise HTTPException(
            403,
            "review writes are disabled; restart with `ceskb serve --allow-review` to "
            "record decisions from the UI, or use `ceskb override` on the command line",
        )

    from ceskb.classify.engine import classify_all
    from ceskb.review.overrides import (
        KEEP_CONCEPT,
        Override,
        OverrideError,
        check_overrides,
        record_override,
    )
    from ceskb.store.db import connect
    from ceskb.vocab.loader import load_vocabulary

    outcome_uid = (payload.get("outcome_uid") or "").strip()
    reason = (payload.get("reason") or "").strip()
    reviewer = (payload.get("reviewer") or "").strip()
    axes = payload.get("axes") or {}
    if not outcome_uid or not reviewer:
        raise HTTPException(400, "outcome_uid and reviewer are required")
    if len(reason) < 8:
        raise HTTPException(400, "a reason of at least 8 characters is required")
    if not isinstance(axes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in axes.items()
    ):
        raise HTTPException(400, "axes must be a mapping of axis_id to term_id")

    # "concept_id" absent means no opinion; present and null means nothing fits.
    concept_id = payload["concept_id"] if "concept_id" in payload else KEEP_CONCEPT
    if concept_id is KEEP_CONCEPT and not axes:
        raise HTTPException(400, "nothing to record: give a concept_id or at least one axis")

    # Check the names resolve before anything is written. record_override commits to a
    # git-tracked file, so a bad concept id must fail here rather than leave a decision
    # on disk that can never apply.
    problems = check_overrides(
        [
            Override(
                outcome_uid=outcome_uid,
                reason=reason,
                reviewer=reviewer,
                decided_at="",
                concept_id=concept_id,
                axes=axes,
            )
        ],
        load_vocabulary(),
    )
    if problems:
        raise HTTPException(400, "; ".join(problems))

    with connect(PATHS.database) as conn:
        try:
            override = record_override(
                conn,
                outcome_uid=outcome_uid,
                reason=reason,
                reviewer=reviewer,
                concept_id=concept_id,
                axes=axes,
                notes=payload.get("notes"),
            )
            stats = classify_all(conn)
        except OverrideError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"recorded": override.to_dict(), "reclassified": stats}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "review_writes": str(_review_writes_allowed()).lower()}


if PATHS.web.exists():
    app.mount("/static", StaticFiles(directory=str(PATHS.web / "static")), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(PATHS.web / "index.html"))
