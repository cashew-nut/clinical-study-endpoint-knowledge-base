"""Pipeline, provenance and USDM projection."""

from __future__ import annotations

import json

import pytest

from ceskb.classify.engine import classify_all
from ceskb.ingest.normalise import FIELD_PATHS, dig, probe_schema
from ceskb.ingest.pipeline import ingest
from ceskb.ingest.sources import FixtureSource
from ceskb.project.usdm import (
    ENDPOINT_LEVEL_CODES,
    OBJECTIVE_LEVEL_CODES,
    USDM_VERSION,
    UsdmProjector,
)
from ceskb.store.db import connect, initialise, load_vocabulary_into_db


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def test_fixture_corpus_ingests(conn):
    assert conn.execute("SELECT count(*) FROM study").fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM study_outcome").fetchone()[0] > 0


def test_fixtures_are_marked_synthetic(conn):
    total, synthetic = conn.execute(
        "SELECT count(*), sum(CASE WHEN conditions LIKE '%__SYNTHETIC_FIXTURE__%' "
        "THEN 1 ELSE 0 END) FROM study"
    ).fetchone()
    assert synthetic == total, "every fixture study must be identifiable as synthetic"


def test_declared_field_paths_all_resolve_against_the_fixture_shape():
    """Guards the contract between the source shape and the normaliser."""
    records = list(FixtureSource().iter_studies())
    result = probe_schema(records)
    absent = [name for name, info in result.items() if info["present"] == 0]
    assert not absent, f"declared paths that never resolved: {absent}"


def test_dig_returns_none_rather_than_raising():
    assert dig({"a": {"b": 1}}, "a.b") == 1
    assert dig({"a": {"b": 1}}, "a.c.d") is None
    assert dig({}, FIELD_PATHS["study_id"]) is None


def test_reingest_is_idempotent(tmp_path, vocab):
    path = tmp_path / "idem.duckdb"
    with connect(path) as conn:
        initialise(conn)
        load_vocabulary_into_db(conn, vocab)
        first = ingest(conn, FixtureSource(), write_bronze=False)
        second = ingest(conn, FixtureSource(), write_bronze=False)
        studies = conn.execute("SELECT count(*) FROM study").fetchone()[0]
        outcomes = conn.execute("SELECT count(*) FROM study_outcome").fetchone()[0]

    assert first.studies_inserted > 0
    assert second.studies_inserted == 0
    assert second.studies_unchanged == first.studies_seen
    assert second.outcomes_written == 0
    assert studies == first.studies_seen
    assert outcomes == first.outcomes_written


def test_watermark_is_recorded(tmp_path, vocab):
    from ceskb.store.db import get_watermark

    path = tmp_path / "wm.duckdb"
    with connect(path) as conn:
        initialise(conn)
        load_vocabulary_into_db(conn, vocab)
        ingest(conn, FixtureSource(), write_bronze=False)
        assert get_watermark(conn, "fixtures", "last_update_posted") is not None


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
def test_every_spec_has_rule_evidence(conn):
    orphans = conn.execute(
        "SELECT count(*) FROM endpoint_spec e WHERE NOT EXISTS "
        "(SELECT 1 FROM classification_evidence c WHERE c.spec_id = e.spec_id AND c.selected)"
    ).fetchone()[0]
    assert orphans == 0


def test_every_spec_axis_has_a_known_origin(conn):
    bad = conn.execute(
        "SELECT DISTINCT origin FROM endpoint_spec_axis WHERE origin NOT IN "
        "('concept_default', 'rule_assert', 'extracted', 'unresolved')"
    ).fetchall()
    assert not bad


def test_spec_axis_terms_exist_in_the_vocabulary(conn):
    unknown = conn.execute(
        """
        SELECT DISTINCT a.axis_id, a.term_id
        FROM endpoint_spec_axis a
        LEFT JOIN term t ON t.axis_id = a.axis_id AND t.term_id = a.term_id
        WHERE t.term_id IS NULL
        """
    ).fetchall()
    assert not unknown, f"specs reference terms that do not exist: {unknown}"


def test_every_outcome_is_either_classified_or_recorded_as_unclassified(conn):
    outcomes, classified, unclassified = conn.execute(
        "SELECT (SELECT count(*) FROM study_outcome), "
        "(SELECT count(*) FROM endpoint_spec), (SELECT count(*) FROM unclassified_outcome)"
    ).fetchone()
    assert classified + unclassified == outcomes


def test_coverage_is_reported_and_reasonable(conn):
    outcomes, classified = conn.execute(
        "SELECT (SELECT count(*) FROM study_outcome), (SELECT count(*) FROM endpoint_spec)"
    ).fetchone()
    assert classified / outcomes > 0.85


def test_reclassification_is_deterministic(tmp_path, vocab):
    path = tmp_path / "det.duckdb"
    with connect(path) as conn:
        initialise(conn)
        load_vocabulary_into_db(conn, vocab)
        ingest(conn, FixtureSource(), write_bronze=False)
        first = classify_all(conn, vocab=vocab)
        snapshot_a = conn.execute(
            "SELECT spec_id, concept_id FROM endpoint_spec ORDER BY spec_id"
        ).fetchall()
        second = classify_all(conn, vocab=vocab)
        snapshot_b = conn.execute(
            "SELECT spec_id, concept_id FROM endpoint_spec ORDER BY spec_id"
        ).fetchall()
    assert first == second
    assert snapshot_a == snapshot_b


# --------------------------------------------------------------------------- #
# USDM projection
# --------------------------------------------------------------------------- #
def test_every_classified_study_projects(conn):
    studies_with_specs = conn.execute(
        "SELECT count(DISTINCT study_id) FROM endpoint_spec"
    ).fetchone()[0]
    projections = conn.execute("SELECT count(*) FROM usdm_projection").fetchone()[0]
    assert projections == studies_with_specs


def test_projection_uses_real_cdisc_endpoint_level_codes(conn):
    document = json.loads(
        conn.execute("SELECT document FROM usdm_projection LIMIT 1").fetchone()[0]
    )
    design = document["study"]["versions"][0]["studyDesigns"][0]
    valid_endpoint = {code for code, _ in ENDPOINT_LEVEL_CODES.values()}
    valid_objective = {code for code, _ in OBJECTIVE_LEVEL_CODES.values()}
    assert design["objectives"]
    for objective in design["objectives"]:
        assert objective["level"]["code"] in valid_objective
        assert objective["endpoints"]
        for endpoint in objective["endpoints"]:
            assert endpoint["level"]["code"] in valid_endpoint


def test_every_template_tag_resolves_through_a_parameter_map(conn):
    """A dangling [Tag] would make the document unrenderable."""
    import re

    rows = conn.execute("SELECT study_id, document FROM usdm_projection").fetchall()
    assert rows
    for study_id, raw in rows:
        design = json.loads(raw)["study"]["versions"][0]["studyDesigns"][0]
        dictionaries = {d["id"]: d for d in design["dictionaries"]}
        for objective in design["objectives"]:
            for endpoint in objective["endpoints"]:
                tags = set(re.findall(r"\[([^\]]+)\]", endpoint["text"]))
                dictionary = dictionaries[endpoint["dictionaryId"]]
                mapped = {p["tag"] for p in dictionary["parameterMaps"]}
                assert tags <= mapped, f"{study_id}: unmapped tags {tags - mapped}"


def test_estimand_references_a_real_endpoint_and_population(conn):
    rows = conn.execute("SELECT document FROM usdm_projection").fetchall()
    for (raw,) in rows:
        design = json.loads(raw)["study"]["versions"][0]["studyDesigns"][0]
        endpoint_ids = {
            e["id"] for o in design["objectives"] for e in o["endpoints"]
        }
        population_ids = {p["id"] for p in design["analysisPopulations"]}
        for estimand in design["estimands"]:
            assert estimand["variableOfInterestId"] in endpoint_ids
            assert estimand["analysisPopulationId"] in population_ids


def test_intercurrent_events_are_marked_unspecified_not_invented(conn):
    """Registry text never states a strategy, so claiming one would be fabrication."""
    (raw,) = conn.execute("SELECT document FROM usdm_projection LIMIT 1").fetchone()
    design = json.loads(raw)["study"]["versions"][0]["studyDesigns"][0]
    for estimand in design["estimands"]:
        assert estimand["intercurrentEvents"]
        for event in estimand["intercurrentEvents"]:
            assert event["strategy"] == "unspecified"


def test_projection_declares_its_usdm_version(conn):
    (raw,) = conn.execute("SELECT document FROM usdm_projection LIMIT 1").fetchone()
    assert json.loads(raw)["usdmVersion"] == USDM_VERSION


def test_iso_duration_encoding(vocab):
    from ceskb.project.usdm import _iso_duration

    assert _iso_duration(12, "week") == "P12W"
    assert _iso_duration(6, "month") == "P6M"
    assert _iso_duration(28, "day") == "P28D"
    assert _iso_duration(None, "week") is None
    assert _iso_duration(12, None) is None


usdm_model = pytest.importorskip(
    "usdm_model", reason="CDISC usdm package not installed; run pip install usdm"
)


def test_projection_validates_against_the_cdisc_usdm_model(conn):
    """The strongest available check: CDISC's own pydantic classes accept our output."""
    from usdm_model.activity import Activity
    from usdm_model.analysis_population import AnalysisPopulation
    from usdm_model.endpoint import Endpoint
    from usdm_model.estimand import Estimand
    from usdm_model.objective import Objective
    from usdm_model.syntax_template_dictionary import SyntaxTemplateDictionary
    from usdm_model.timing import Timing

    rows = conn.execute("SELECT study_id, document FROM usdm_projection").fetchall()
    assert rows
    for study_id, raw in rows:
        design = json.loads(raw)["study"]["versions"][0]["studyDesigns"][0]
        for objective in design["objectives"]:
            Objective(**objective)
            for endpoint in objective["endpoints"]:
                Endpoint(**endpoint)
        for estimand in design["estimands"]:
            Estimand(**estimand)
        for population in design["analysisPopulations"]:
            AnalysisPopulation(**population)
        for activity in design["activities"]:
            Activity(**activity)
        for timing in design["scheduleTimelines"][0]["timings"]:
            Timing(**timing)
        for dictionary in design["dictionaries"]:
            SyntaxTemplateDictionary(**dictionary)
