"""The read-only USDM 4.0 HTTP surface.

One call is what this exists for: GET /v4/studies/{nctId}/endpoints.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clinical_endpoints.usdm.api import create_app


@pytest.fixture
def client(usdm_warehouse_path):
    return TestClient(create_app(usdm_warehouse_path))


def test_the_one_call_returns_every_endpoint_in_the_trial(client):
    response = client.get("/v4/studies/NCT00000001/endpoints")
    assert response.status_code == 200
    assert response.headers["X-USDM-Version"] == "4.0.0"
    body = response.json()
    assert body["usdmVersion"] == "4.0.0"
    assert body["study"]["nctId"] == "NCT00000001"
    assert sum(len(o["endpoints"]) for o in body["objectives"]) == 5
    assert body["provenance"]["tiers"] == {"partial": 1, "templated": 3, "verbatim": 1}


def test_the_response_is_cacheable_by_content(client):
    """The projection is deterministic, so the ETag is a content hash."""
    first = client.get("/v4/studies/NCT00000001/endpoints")
    second = client.get("/v4/studies/NCT00000001/endpoints")
    assert first.headers["ETag"] == second.headers["ETag"]
    assert first.headers["ETag"] != client.get(
        "/v4/studies/NCT00000002/endpoints"
    ).headers["ETag"]


def test_flatten_returns_a_flat_endpoint_list(client):
    body = client.get("/v4/studies/NCT00000001/endpoints?flatten=true").json()
    assert "objectives" not in body
    assert [e["name"] for e in body["endpoints"]] == ["END1", "END2", "END3", "END4", "END5"]


def test_level_and_tier_filters(client):
    body = client.get("/v4/studies/NCT00000001/endpoints?level=primary").json()
    assert [o["level"]["decode"] for o in body["objectives"]] == ["Primary Objective"]

    body = client.get("/v4/studies/NCT00000001/endpoints?tier=verbatim").json()
    assert body["provenance"]["tiers"] == {"verbatim": 1}


def test_the_wrapper_envelope_is_available(client):
    body = client.get("/v4/studies/NCT00000001/endpoints?envelope=wrapper").json()
    assert body["study"]["instanceType"] == "Study"
    assert body["study"]["versions"][0]["studyDesigns"][0]["instanceType"] == (
        "InterventionalStudyDesign"
    )
    assert body["provenance"]["synthesized"]


def test_an_unpulled_trial_is_404_not_an_empty_document(client):
    response = client.get("/v4/studies/NCT09999999/endpoints")
    assert response.status_code == 404
    assert "NCT09999999" in response.json()["detail"]


def test_a_trial_with_no_registered_outcomes_is_200_and_empty(client):
    """Distinct from 404: the trial exists and registered nothing."""
    body = client.get("/v4/studies/NCT00000003/endpoints").json()
    assert body["objectives"] == []


def test_one_endpoint_comes_back_with_its_dictionary(client):
    body = client.get("/v4/studies/NCT00000001/endpoints/END1").json()
    assert body["endpoint"]["name"] == "END1"
    assert body["dictionaries"][0]["id"] == body["endpoint"]["dictionaryId"]
    assert client.get("/v4/studies/NCT00000001/endpoints/END99").status_code == 404


def test_coverage_reports_the_tier_mix(client):
    body = client.get("/v4/studies/NCT00000001/endpoints/coverage").json()
    assert body["endpoints"] == 5
    assert body["tiers"]["templated"] == 3


def test_a_vocabulary_reference_resolves_over_http(client):
    """What makes the dictionary a real "reference source that provides a
    listing of valid parameter names and values" (CT C207597)."""
    body = client.get("/v4/vocab/measurement/pasi").json()
    assert body["term"]["id"] == "pasi"
    assert body["term"]["concept"] == "psoriasis_severity"
    assert "PASI" in body["synonyms"]
    assert client.get("/v4/vocab/measurement/not_a_term").status_code == 404
    assert client.get("/v4/vocab/nonsense/pasi").status_code == 404


def test_a_surrogate_reference_points_at_a_live_vocab_route(client):
    body = client.get("/v4/studies/NCT00000001/endpoints").json()
    for surrogate in body["bcSurrogates"]:
        assert client.get(surrogate["reference"]).status_code == 200


def test_a_concept_reference_resolves_to_the_measurements_that_share_it(client):
    """`concept` is a column on vocab.measurements, not a table -- but a
    {concept} tag's surrogate references this route, so it has to resolve."""
    body = client.get("/v4/vocab/concept/psoriasis_severity").json()
    assert body["term"]["id"] == "psoriasis_severity"
    assert "pasi" in [m["id"] for m in body["measurements"]]
    assert client.get("/v4/vocab/concept/not_a_concept").status_code == 404
