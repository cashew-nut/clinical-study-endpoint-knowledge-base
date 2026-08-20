"""The read-only USDM 4.0 endpoints API.

One call is the point of this module:

    GET /v4/studies/{nctId}/endpoints

Read-only by design. USDM's own API defines `POST`/`PUT /v4/studyDefinitions`,
and implementing those would make this a study definitions repository of
record. It is a projection of public registry data, and should never pretend
otherwise.

FastAPI and uvicorn are an optional extra (`uv sync --extra serve`); nothing
else in the package imports this module, so the pipeline installs without a web
stack.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response

from clinical_endpoints.db import connect
from clinical_endpoints.usdm import codes
from clinical_endpoints.usdm.codes import UnknownOutcomeType
from clinical_endpoints.usdm.envelope import module_envelope, wrapper_envelope
from clinical_endpoints.usdm.project import (
    NotConformed,
    NotPulled,
    load_projection_rules,
    project,
)

VOCAB_DIMENSIONS = {
    "measurement": "measurements",
    "reference": "references",
    "scale": "scales",
    "form": "forms",
    "direction": "directions",
    "therapeutic_area": "therapeutic_areas",
    "event": "events",
}


def _etag(body: dict) -> str:
    """The projection is deterministic, so a content hash is a valid ETag.

    `provenance.projectedAt` is excluded: it is when this response was built,
    not what it says, and including it would make every response a cache miss.
    """
    content = dict(body)
    if isinstance(content.get("provenance"), dict):
        content["provenance"] = {
            k: v for k, v in content["provenance"].items() if k != "projectedAt"
        }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f'"{digest[:32]}"'


def _json_response(body: dict) -> Response:
    payload = json.dumps(body, ensure_ascii=False)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"X-USDM-Version": codes.USDM_VERSION, "ETag": _etag(body)},
    )


def create_app(warehouse: str = "warehouse.duckdb") -> FastAPI:
    app = FastAPI(
        title="Clinical endpoints USDM 4.0 API",
        description=(
            "A read-only CDISC USDM 4.0 projection of ClinicalTrials.gov endpoints, "
            "keyed on NCT id. Each endpoint's text is a syntax template whose tags "
            "resolve, through its SyntaxTemplateDictionary, into controlled "
            "vocabularies.\n\n"
            "Note the deviation from the USDM API's own routes: USDM keys study "
            "resources on a repository UUID (`/v4/studyDefinitions/{studyId}`). "
            "This service has no such repository -- it has a registry mirror, and "
            "the identifier its users hold is an NCT id -- so the route keys on "
            "that. A client holding a USDM study UUID from elsewhere will not "
            "find it here."
        ),
        version=codes.USDM_VERSION,
    )

    def _project(nct_id: str, levels, tiers):
        con = connect(warehouse)
        try:
            rules = load_projection_rules(con)
            projection = project(con, nct_id, rules=rules, levels=levels, tiers=tiers)
            return con, rules, projection
        except NotPulled as exc:
            con.close()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotConformed as exc:
            con.close()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnknownOutcomeType as exc:
            con.close()
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v4/studies/{nct_id}/endpoints", tags=["Endpoints"])
    def read_endpoints(
        nct_id: str,
        envelope: str = Query("module", pattern="^(module|wrapper)$"),
        flatten: bool = False,
        level: Optional[list[str]] = Query(None),
        tier: Optional[list[str]] = Query(None),
    ) -> Response:
        """Every endpoint in the trial, as USDM 4.0."""
        con, rules, projection = _project(nct_id, level, tier)
        try:
            if envelope == "wrapper":
                body = wrapper_envelope(con, projection, vocab_version=rules.vocab_version)
            else:
                body = module_envelope(
                    con, projection, vocab_version=rules.vocab_version, flatten=flatten
                )
        finally:
            con.close()
        return _json_response(body)

    @app.get("/v4/studies/{nct_id}/endpoints/coverage", tags=["Endpoints"])
    def read_coverage(nct_id: str) -> Response:
        """The fidelity-tier mix for one trial."""
        con, _rules, projection = _project(nct_id, None, None)
        con.close()
        return _json_response(
            {
                "nctId": nct_id,
                "endpoints": projection.endpoint_count,
                "tiers": dict(sorted(projection.tiers.items())),
            }
        )

    @app.get("/v4/studies/{nct_id}/endpoints/{endpoint_name}", tags=["Endpoints"])
    def read_endpoint(nct_id: str, endpoint_name: str) -> Response:
        """One endpoint, by `name` (END1, END2, ...), with its dictionary."""
        con, rules, projection = _project(nct_id, None, None)
        con.close()
        for endpoint in projection.endpoints():
            if endpoint["name"] == endpoint_name or endpoint["id"] == endpoint_name:
                dictionaries = [
                    d for d in projection.dictionaries if d["id"] == endpoint["dictionaryId"]
                ]
                return _json_response(
                    {
                        "usdmVersion": codes.USDM_VERSION,
                        "study": {"id": projection.study_id, "nctId": nct_id},
                        "endpoint": endpoint,
                        "dictionaries": dictionaries,
                        "bcSurrogates": projection.bc_surrogates,
                    }
                )
        raise HTTPException(status_code=404, detail=f"{nct_id} has no endpoint {endpoint_name!r}")

    @app.get("/v4/vocab/concept/{concept}", tags=["Vocabulary"])
    def read_concept(concept: str) -> Response:
        """The measurements sharing one concept.

        `concept` is a column on `vocab.measurements`, not a table of its own --
        measurements.yaml keeps the underlying construct alongside the
        instrument so "same concept, different instrument" (PASI vs sPGA vs BSA)
        is one query. A `{concept}` tag's surrogate references this route, so it
        has to resolve like any other.
        """
        con = connect(warehouse)
        try:
            rows = con.execute(
                "SELECT id, label, inline_label, domain FROM vocab.measurements "
                "WHERE concept = ? ORDER BY id",
                [concept],
            ).fetchall()
        finally:
            con.close()
        if not rows:
            raise HTTPException(status_code=404, detail=f"no concept {concept!r}")
        return _json_response(
            {
                "dimension": "concept",
                "term": {"id": concept, "label": concept.replace("_", " ")},
                "measurements": [
                    {"id": r[0], "label": r[1], "inlineLabel": r[2], "domain": r[3]} for r in rows
                ],
            }
        )

    @app.get("/v4/vocab/{dimension}/{term_id}", tags=["Vocabulary"])
    def read_vocab_term(dimension: str, term_id: str) -> Response:
        """Resolve a `BiomedicalConceptSurrogate.reference` back to its term.

        This is what makes the dictionary what CT says a SyntaxTemplateDictionary
        should be: "a reference source that provides a listing of valid parameter
        names and values" (C207597).
        """
        table = VOCAB_DIMENSIONS.get(dimension)
        if table is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown dimension {dimension!r}; known: {sorted(VOCAB_DIMENSIONS)}",
            )
        con = connect(warehouse)
        try:
            columns = [
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'vocab' AND table_name = ? ORDER BY ordinal_position",
                    [table],
                ).fetchall()
            ]
            if not columns:
                raise HTTPException(
                    status_code=409, detail="vocab.* is empty -- run `endpoints vocab validate`"
                )
            row = con.execute(
                f"SELECT {', '.join(columns)} FROM vocab.{table} WHERE id = ?", [term_id]
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"no {dimension} {term_id!r}")
            term = dict(zip(columns, row))
            synonyms = [
                r[0]
                for r in con.execute(
                    "SELECT synonym FROM vocab.synonyms WHERE dimension = ? AND term_id = ? "
                    "ORDER BY synonym",
                    [dimension, term_id],
                ).fetchall()
            ]
        finally:
            con.close()
        return _json_response({"dimension": dimension, "term": term, "synonyms": synonyms})

    return app
