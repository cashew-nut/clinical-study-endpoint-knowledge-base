"""Export artefacts.

The database is convenient but is not the interchange format. Exports give three
audiences what they each need: Parquet for analysts, USDM JSON for study-exchange
tooling, and an edge list for anyone who wants the knowledge base in a graph store.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from ceskb.config import PATHS

#: Tables worth exporting as Parquet. Evidence tables are included deliberately:
#: an export without provenance is not reproducible.
PARQUET_TABLES = (
    "axis",
    "term",
    "term_external_mapping",
    "concept",
    "concept_structure",
    "concept_threshold",
    "concept_relation",
    "study",
    "study_outcome",
    "endpoint_spec",
    "endpoint_spec_axis",
    "classification_evidence",
    "extraction_evidence",
    "unclassified_outcome",
)


def export_parquet(conn: duckdb.DuckDBPyConnection, out: Path) -> list[Path]:
    target = out / "parquet"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for table in PARQUET_TABLES:
        path = target / f"{table}.parquet"
        conn.execute(f"COPY {table} TO '{path}' (FORMAT PARQUET)")
        written.append(path)
    return written


def export_usdm(conn: duckdb.DuckDBPyConnection, out: Path) -> list[Path]:
    target = out / "usdm"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for study_id, document in conn.execute(
        "SELECT study_id, document FROM usdm_projection ORDER BY study_id"
    ).fetchall():
        path = target / f"{study_id}.usdm.json"
        path.write_text(json.dumps(json.loads(document), indent=2, ensure_ascii=False))
        written.append(path)
    return written


def export_graph(conn: duckdb.DuckDBPyConnection, out: Path) -> list[Path]:
    """Write nodes and edges for loading into a graph store.

    The knowledge base is stored relationally because the queries it serves are joins
    of bounded depth, but the shape is a graph and some consumers will want it as one.
    This export is the bridge, not a second system of record.
    """
    target = out / "graph"
    target.mkdir(parents=True, exist_ok=True)

    nodes = conn.execute(
        """
        SELECT 'concept:' || concept_id AS id, 'Concept' AS kind, label FROM concept
        UNION ALL
        SELECT 'term:' || axis_id || ':' || term_id, 'Term', label FROM term
        UNION ALL
        SELECT 'axis:' || axis_id, 'Axis', label FROM axis
        UNION ALL
        SELECT 'study:' || study_id, 'Study', coalesce(brief_title, study_id) FROM study
        """
    ).fetchall()

    edges = conn.execute(
        """
        SELECT 'term:' || axis_id || ':' || term_id, 'IN_AXIS', 'axis:' || axis_id, NULL FROM term
        UNION ALL
        SELECT 'concept:' || concept_id, 'HAS_' || upper(axis_id),
               'term:' || axis_id || ':' || term_id, role
        FROM concept_structure
        UNION ALL
        SELECT 'concept:' || concept_id, 'RELATED_' || upper(relation),
               'concept:' || related_concept_id, NULL
        FROM concept_relation
        UNION ALL
        SELECT 'study:' || study_id, 'MEASURES', 'concept:' || concept_id, endpoint_level
        FROM endpoint_spec
        """
    ).fetchall()

    node_path = target / "nodes.jsonl"
    edge_path = target / "edges.jsonl"
    with node_path.open("w") as handle:
        for node_id, kind, label in nodes:
            handle.write(json.dumps({"id": node_id, "kind": kind, "label": label}) + "\n")
    with edge_path.open("w") as handle:
        for source, relation, target_id, qualifier in edges:
            handle.write(
                json.dumps(
                    {"from": source, "rel": relation, "to": target_id, "qualifier": qualifier}
                )
                + "\n"
            )
    return [node_path, edge_path]


def export_all(conn: duckdb.DuckDBPyConnection, out: Path | None = None) -> list[Path]:
    out = out or PATHS.exports
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    written.extend(export_parquet(conn, out))
    written.extend(export_usdm(conn, out))
    written.extend(export_graph(conn, out))
    return written
