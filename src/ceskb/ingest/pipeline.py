"""Ingest orchestration: raw snapshot to study and outcome rows.

Records land in three stages. Bronze is the untouched payload written to disk and
hashed, so any derived row can be re-derived from exactly the bytes that produced it.
Silver is the normalised study and outcome tables. Gold is Layer B, produced by the
classifier in `ceskb.classify`.

Re-running is cheap and safe: rows are keyed by content hash, so an unchanged study is
touched (last_seen_at moves) but not rewritten, and only changed studies invalidate
their derived specifications.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import duckdb

from ceskb.config import PATHS
from ceskb.ingest.normalise import (
    NormalisedStudy,
    TherapeuticAreaInferrer,
    normalise_study,
    probe_schema,
)
from ceskb.ingest.sources import StudySource
from ceskb.store.db import _json, finish_run, set_watermark, start_run, utcnow
from ceskb.vocab.loader import load_vocabulary


@dataclass
class IngestStats:
    studies_seen: int = 0
    studies_inserted: int = 0
    studies_updated: int = 0
    studies_unchanged: int = 0
    outcomes_written: int = 0
    max_last_update_posted: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "studies_seen": self.studies_seen,
            "studies_inserted": self.studies_inserted,
            "studies_updated": self.studies_updated,
            "studies_unchanged": self.studies_unchanged,
            "outcomes_written": self.outcomes_written,
            "max_last_update_posted": self.max_last_update_posted,
        }


def _write_bronze(source_name: str, records: list[dict[str, Any]], snapshot_id: str) -> Path:
    PATHS.bronze.mkdir(parents=True, exist_ok=True)
    safe = source_name.replace("/", "_")
    path = PATHS.bronze / f"{safe}_{snapshot_id}.json"
    path.write_text(json.dumps({"studies": records}, ensure_ascii=False))
    return path


def _upsert_study(
    conn: duckdb.DuckDBPyConnection, study: NormalisedStudy, snapshot_id: str, stats: IngestStats
) -> None:
    now = utcnow()
    existing = conn.execute(
        "SELECT record_hash FROM study WHERE study_id = ?", [study.study_id]
    ).fetchone()

    if existing and existing[0] == study.record_hash:
        conn.execute("UPDATE study SET last_seen_at = ? WHERE study_id = ?", [now, study.study_id])
        stats.studies_unchanged += 1
        return

    conditions = list(study.conditions)
    if study.is_synthetic:
        # Marked in the record so synthetic demonstration data is never mistaken for
        # registry data by anything reading the database.
        conditions = conditions + ["__SYNTHETIC_FIXTURE__"]

    if existing:
        conn.execute("DELETE FROM study WHERE study_id = ?", [study.study_id])
        conn.execute("DELETE FROM study_ta_evidence WHERE study_id = ?", [study.study_id])
        stats.studies_updated += 1
        first_seen = conn.execute(
            "SELECT min(first_seen_at) FROM study_outcome WHERE study_id = ?", [study.study_id]
        ).fetchone()[0] or now
    else:
        stats.studies_inserted += 1
        first_seen = now

    conn.execute(
        """
        INSERT INTO study VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            study.study_id,
            study.source,
            study.brief_title,
            study.official_title,
            study.overall_status,
            study.study_type,
            _json(study.phases),
            study.enrollment,
            study.lead_sponsor,
            study.sponsor_class,
            _json(conditions),
            _json(study.therapeutic_areas),
            study.start_date,
            study.primary_completion_date,
            study.completion_date,
            study.last_update_posted,
            study.has_results,
            first_seen,
            now,
            study.record_hash,
            snapshot_id,
        ],
    )

    for area, matched_term, matched_in in study.ta_evidence:
        conn.execute(
            """
            INSERT INTO study_ta_evidence VALUES (?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [study.study_id, area, matched_term, matched_in],
        )

    for outcome in study.outcomes:
        prior = conn.execute(
            "SELECT record_hash, first_seen_at FROM study_outcome WHERE outcome_uid = ?",
            [outcome.outcome_uid],
        ).fetchone()
        if prior and prior[0] == outcome.record_hash:
            conn.execute(
                "UPDATE study_outcome SET last_seen_at = ? WHERE outcome_uid = ?",
                [now, outcome.outcome_uid],
            )
            continue
        outcome_first_seen = prior[1] if prior else now
        if prior:
            # The outcome's text changed, so any specification derived from it is stale.
            conn.execute("DELETE FROM study_outcome WHERE outcome_uid = ?", [outcome.outcome_uid])
            conn.execute("DELETE FROM endpoint_spec WHERE outcome_uid = ?", [outcome.outcome_uid])
            conn.execute("DELETE FROM unclassified_outcome WHERE outcome_uid = ?", [outcome.outcome_uid])
        conn.execute(
            "INSERT INTO study_outcome VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                outcome.outcome_uid,
                outcome.study_id,
                outcome.endpoint_level,
                outcome.ordinal,
                outcome.measure,
                outcome.description,
                outcome.time_frame,
                outcome.record_hash,
                outcome_first_seen,
                now,
            ],
        )
        stats.outcomes_written += 1


def _batched(iterator: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for item in iterator:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def ingest(
    conn: duckdb.DuckDBPyConnection,
    source: StudySource,
    batch_size: int = 500,
    write_bronze: bool = True,
) -> IngestStats:
    """Pull every record the source offers and persist studies and outcomes."""
    vocab = load_vocabulary()
    inferrer = TherapeuticAreaInferrer(vocab)
    stats = IngestStats()
    run_id = uuid.uuid4().hex[:16]
    start_run(conn, run_id, "ingest")

    try:
        for batch in _batched(iter(source.iter_studies()), batch_size):
            snapshot_id = uuid.uuid4().hex[:16]
            file_path = str(_write_bronze(source.name, batch, snapshot_id)) if write_bronze else None
            payload_hash = _json(batch)
            conn.execute(
                "INSERT INTO source_snapshot VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    snapshot_id,
                    source.name,
                    utcnow(),
                    _json(source.describe()),
                    len(batch),
                    str(hash(payload_hash)),
                    file_path,
                ],
            )

            for record in batch:
                study = normalise_study(record, source.name, inferrer)
                if study is None:
                    continue
                stats.studies_seen += 1
                _upsert_study(conn, study, snapshot_id, stats)
                if study.last_update_posted:
                    if (
                        stats.max_last_update_posted is None
                        or study.last_update_posted > stats.max_last_update_posted
                    ):
                        stats.max_last_update_posted = study.last_update_posted

        if stats.max_last_update_posted:
            set_watermark(
                conn, source.name, "last_update_posted", stats.max_last_update_posted
            )
        finish_run(conn, run_id, "succeeded", stats.as_dict())
    except Exception as exc:  # noqa: BLE001 - recorded then re-raised
        finish_run(conn, run_id, "failed", stats.as_dict(), error=str(exc))
        raise

    return stats


def probe(source: StudySource, limit: int = 50) -> dict[str, dict[str, Any]]:
    """Check the source's schema against the declared field paths without persisting."""
    records: list[dict[str, Any]] = []
    for record in source.iter_studies():
        records.append(record)
        if len(records) >= limit:
            break
    return probe_schema(records)
