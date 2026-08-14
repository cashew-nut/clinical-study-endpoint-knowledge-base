"""DuckDB access and vocabulary loading.

The database is a derived artefact: the YAML vocabularies and rule packs in git are
the system of record, and `load_vocabulary_into_db` rebuilds the Layer A tables from
them wholesale. Source and derived tables are never rebuilt this way -- they are
accumulated incrementally and keyed by content hash.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb

from ceskb.config import PATHS
from ceskb.vocab.loader import STRUCTURE_AXES, Vocabulary, load_vocabulary

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def connect(path: Path | str | None = None, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    target = Path(path) if path is not None else PATHS.database
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(target), read_only=read_only)
    try:
        yield conn
    finally:
        conn.close()


def initialise(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables and views. Safe to run repeatedly."""
    conn.execute(SCHEMA_SQL.read_text())


def _as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def load_vocabulary_into_db(
    conn: duckdb.DuckDBPyConnection, vocab: Vocabulary | None = None
) -> dict[str, int]:
    """Replace the Layer A tables with the current contents of the YAML sources."""
    vocab = vocab or load_vocabulary()

    for table in (
        "axis",
        "term",
        "term_external_mapping",
        "concept",
        "concept_structure",
        "concept_therapeutic_area",
        "concept_threshold",
        "concept_component",
        "concept_relation",
        "concept_criteria",
        "rule",
    ):
        conn.execute(f"DELETE FROM {table}")

    for axis in vocab.axes.values():
        conn.execute(
            "INSERT INTO axis VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                axis.axis_id,
                axis.label,
                axis.definition,
                axis.extensible,
                axis.usdm_alignment.get("entity"),
                axis.usdm_alignment.get("attribute"),
                axis.usdm_alignment.get("codelist_c_code"),
            ],
        )
        for term in axis.terms.values():
            conn.execute(
                "INSERT INTO term VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    term.axis_id,
                    term.term_id,
                    term.label,
                    term.definition,
                    _json(list(term.synonyms)),
                    term.broader,
                    _json(term.attributes),
                    term.status,
                    term.notes,
                ],
            )
            for mapping in term.external_mappings:
                conn.execute(
                    "INSERT INTO term_external_mapping VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        term.axis_id,
                        term.term_id,
                        mapping["system"],
                        mapping["code"],
                        mapping.get("display"),
                        mapping.get("system_version"),
                        mapping["verified"],
                        mapping.get("verified_against"),
                    ],
                )

    for concept in vocab.concepts.values():
        conn.execute(
            "INSERT INTO concept VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                concept.concept_id,
                concept.label,
                concept.abbreviation,
                concept.definition,
                _json(list(concept.synonyms)),
                _json(list(concept.therapeutic_areas)),
                _json(concept.usdm_hints),
                concept.status,
                concept.notes,
                concept.source_file,
            ],
        )
        for key, term_id in concept.structure.items():
            axis_id, role = STRUCTURE_AXES[key]
            conn.execute(
                "INSERT INTO concept_structure VALUES (?, ?, ?, ?)",
                [concept.concept_id, axis_id, term_id, role],
            )
        for ta in concept.therapeutic_areas:
            conn.execute(
                "INSERT INTO concept_therapeutic_area VALUES (?, ?)", [concept.concept_id, ta]
            )
        threshold = concept.definitional_threshold
        if threshold:
            conn.execute(
                "INSERT INTO concept_threshold VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    concept.concept_id,
                    threshold["kind"],
                    threshold["operator"],
                    str(threshold["value"]),
                    _as_number(threshold["value"]),
                    threshold.get("unit"),
                    threshold.get("applies_to"),
                    threshold.get("note"),
                ],
            )
        for ordinal, component in enumerate(concept.components):
            conn.execute(
                "INSERT INTO concept_component VALUES (?, ?, ?, ?, ?, ?)",
                [
                    concept.concept_id,
                    ordinal,
                    component["label"],
                    component.get("concept_id"),
                    component.get("measurement_concept"),
                    component.get("note"),
                ],
            )
        for relation in concept.related_concepts:
            conn.execute(
                "INSERT INTO concept_relation VALUES (?, ?, ?, ?)",
                [
                    concept.concept_id,
                    relation["concept_id"],
                    relation["relation"],
                    relation.get("note"),
                ],
            )
        for criteria in concept.governing_criteria:
            conn.execute(
                "INSERT INTO concept_criteria VALUES (?, ?, ?, ?, ?)",
                [
                    concept.concept_id,
                    criteria["name"],
                    criteria.get("version"),
                    criteria.get("citation"),
                    criteria.get("url"),
                ],
            )

    for rule in vocab.rules:
        conn.execute(
            "INSERT INTO rule VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                rule.rulepack_id,
                rule.rulepack_version,
                rule.rule_id,
                rule.concept_id,
                rule.priority,
                rule.confidence,
                rule.therapeutic_area_hint,
                _json(rule.match),
                _json(rule.asserts),
                rule.notes,
            ],
        )

    return {
        "axes": len(vocab.axes),
        "terms": sum(len(a.terms) for a in vocab.axes.values()),
        "concepts": len(vocab.concepts),
        "rules": len(vocab.rules),
    }


def set_watermark(conn: duckdb.DuckDBPyConnection, source: str, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO watermark (source, key, value, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT (source, key) DO UPDATE SET value = EXCLUDED.value,
                                                updated_at = EXCLUDED.updated_at
        """,
        [source, key, value, utcnow()],
    )


def get_watermark(conn: duckdb.DuckDBPyConnection, source: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM watermark WHERE source = ? AND key = ?", [source, key]
    ).fetchone()
    return row[0] if row else None


def start_run(conn: duckdb.DuckDBPyConnection, run_id: str, stage: str) -> None:
    conn.execute(
        "INSERT INTO pipeline_run (run_id, stage, status, started_at) VALUES (?, ?, 'running', ?)",
        [run_id, stage, utcnow()],
    )


def finish_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    status: str,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE pipeline_run SET status = ?, finished_at = ?, stats = ?, error = ? WHERE run_id = ?",
        [status, utcnow(), _json(stats or {}), error, run_id],
    )
