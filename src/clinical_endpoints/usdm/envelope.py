"""The two response envelopes: the endpoints module, and a full USDM `Wrapper`.

`module` is the default and the right answer for "give me this trial's
endpoints": the endpoint-module objects and nothing else, each a schema-valid
USDM class instance, with provenance.

`wrapper` is a full USDM `Wrapper` for consumers whose tooling only eats one.
It is buildable at all because `raw.studies` now carries the design and
eligibility facts USDM requires (see ingest/design.py) -- before that, a valid
Wrapper meant asserting `StudyDesignPopulation.includesHealthySubjects` and
`InterventionalStudyDesign.model` out of thin air. Anything still unsourceable
is emitted as an empty string or a declared default and named in
`provenance.synthesized[]`: **a placeholder that is not announced is a
fabricated clinical fact.**
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb

from clinical_endpoints.usdm import codes
from clinical_endpoints.usdm.ids import IdFactory
from clinical_endpoints.usdm.project import Projection

#: Where CDISC publishes no mapping for a ClinicalTrials.gov value -- arm type,
#: intervention model, primary purpose -- the Code is scoped to the registry it
#: came from rather than given an invented CDISC C-code. `codeSystem` is a free
#: string in USDM, so this says exactly what it means: this value is
#: ClinicalTrials.gov's, not CDISC CT's.
CTGOV_CODE_SYSTEM = "https://clinicaltrials.gov"

TITLE_BRIEF = ("C207615", "Brief Study Title")
TITLE_OFFICIAL = ("C207616", "Official Study Title")
CHARACTERISTIC_RANDOMISED = ("C46079", "Randomized")
DATA_ORIGIN_WITHIN_STUDY = ("C188866", "Data Generated Within Study")

#: The pilot uses a single "Both" code for a study open to all sexes.
PLANNED_SEX_CODES = {
    "all": ("C49636", "Both"),
    "female": ("C16576", "Female"),
    "male": ("C20197", "Male"),
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _cdisc_code(ids: IdFactory, code: str, decode: str) -> dict:
    return {
        "id": ids.mint("Code"),
        "extensionAttributes": [],
        "code": code,
        "codeSystem": codes.CODE_SYSTEM,
        "codeSystemVersion": codes.CODE_SYSTEM_VERSION,
        "decode": decode,
        "instanceType": "Code",
    }


def _ctgov_code(ids: IdFactory, value: str) -> dict:
    return {
        "id": ids.mint("Code"),
        "extensionAttributes": [],
        "code": value,
        "codeSystem": CTGOV_CODE_SYSTEM,
        "codeSystemVersion": "",
        "decode": value,
        "instanceType": "Code",
    }


def _quantity(ids: IdFactory, value: float | int | None) -> dict | None:
    """Quantity with a null unit, as the CDISC pilot does for enrolment."""
    if value is None:
        return None
    return {
        "id": ids.mint("Quantity"),
        "extensionAttributes": [],
        "value": float(value),
        "unit": None,
        "instanceType": "Quantity",
    }


def provenance(
    con: duckdb.DuckDBPyConnection, projection: Projection, *, vocab_version: str | None = None
) -> dict:
    source = pulled_at = conformed_at = None
    if _has(con, "raw", "_pull_log"):
        row = con.execute(
            "SELECT source, pulled_at FROM raw._pull_log ORDER BY pulled_at DESC LIMIT 1"
        ).fetchone()
        if row:
            source, pulled_at = row[0], row[1].isoformat() if row[1] else None
    if _has(con, "conformed", "endpoints"):
        row = con.execute(
            "SELECT max(conformed_at) FROM conformed.endpoints WHERE nct_id = ?",
            [projection.nct_id],
        ).fetchone()
        conformed_at = row[0].isoformat() if row and row[0] else None
    return {
        "source": source,
        "pulledAt": pulled_at,
        "conformedAt": conformed_at,
        "vocabVersion": vocab_version,
        "projectedAt": _now(),
        "tiers": dict(sorted(projection.tiers.items())),
    }


def _has(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def module_envelope(
    con: duckdb.DuckDBPyConnection,
    projection: Projection,
    *,
    vocab_version: str | None = None,
    flatten: bool = False,
) -> dict:
    body: dict[str, Any] = {
        "usdmVersion": codes.USDM_VERSION,
        "systemName": codes.SYSTEM_NAME,
        "study": {"id": projection.study_id, "nctId": projection.nct_id},
    }
    if flatten:
        body["endpoints"] = projection.endpoints()
    else:
        body["objectives"] = projection.objectives
    body["dictionaries"] = projection.dictionaries
    body["bcSurrogates"] = projection.bc_surrogates
    body["analysisPopulations"] = projection.analysis_populations
    body["provenance"] = provenance(con, projection, vocab_version=vocab_version)
    return body


_STUDY_SELECT = """
SELECT phase, overall_status, study_type, brief_title, official_title,
       intervention_model, primary_purpose, allocation, masking,
       enrollment_count, enrollment_type, healthy_volunteers, gender,
       minimum_age, maximum_age, population_description
FROM raw.studies WHERE nct_id = ?
"""


def wrapper_envelope(
    con: duckdb.DuckDBPyConnection,
    projection: Projection,
    *,
    vocab_version: str | None = None,
) -> dict:
    """A full USDM Wrapper, with every placeholder named in provenance."""
    ids = IdFactory()
    synthesized: list[str] = []
    row = con.execute(_STUDY_SELECT, [projection.nct_id]).fetchone()
    if row is None:
        raise LookupError(f"{projection.nct_id} is not in raw.studies")
    (
        _phase, _status, study_type, brief_title, official_title,
        intervention_model, primary_purpose, allocation, masking,
        enrollment_count, _enrollment_type, healthy_volunteers, gender,
        minimum_age, maximum_age, population_description,
    ) = row

    organization = {
        "id": ids.mint("Organization"),
        "extensionAttributes": [],
        "name": "ClinicalTrials.gov",
        "label": "ClinicalTrials.gov",
        "type": _ctgov_code(ids, "REGISTRY"),
        "identifierScheme": CTGOV_CODE_SYSTEM,
        "identifier": "ClinicalTrials.gov",
        "legalAddress": None,
        "managedSites": [],
        "instanceType": "Organization",
    }

    titles = []
    for text, (code, decode) in ((brief_title, TITLE_BRIEF), (official_title, TITLE_OFFICIAL)):
        if text:
            titles.append(
                {
                    "id": ids.mint("StudyTitle"),
                    "extensionAttributes": [],
                    "text": text,
                    "type": _cdisc_code(ids, code, decode),
                    "instanceType": "StudyTitle",
                }
            )

    arms = []
    if _has(con, "raw", "design_groups"):
        for group_type, title, description in con.execute(
            "SELECT group_type, title, description FROM raw.design_groups "
            "WHERE nct_id = ? ORDER BY title",
            [projection.nct_id],
        ).fetchall():
            arms.append(
                {
                    "id": ids.mint("StudyArm"),
                    "extensionAttributes": [],
                    "name": title or "Arm",
                    "label": title or "",
                    "description": description or "",
                    "type": _ctgov_code(ids, group_type or "OTHER"),
                    "dataOriginDescription": "Data collected from subjects",
                    "dataOriginType": _cdisc_code(ids, *DATA_ORIGIN_WITHIN_STUDY),
                    "populationIds": [],
                    "notes": [],
                    "instanceType": "StudyArm",
                }
            )

    if healthy_volunteers is None:
        synthesized.append(
            "StudyDesignPopulation.includesHealthySubjects = false "
            "(required by USDM; the source record does not state it)"
        )
    planned_sex = PLANNED_SEX_CODES.get((gender or "").strip().lower())
    population = {
        "id": ids.mint("StudyDesignPopulation"),
        "extensionAttributes": [],
        "name": "POP",
        "label": "",
        "description": population_description or "",
        "includesHealthySubjects": bool(healthy_volunteers),
        "plannedEnrollmentNumber": _quantity(ids, enrollment_count),
        "plannedCompletionNumber": None,
        "plannedSex": [_cdisc_code(ids, *planned_sex)] if planned_sex else [],
        "criterionIds": [],
        "plannedAge": _age_range(ids, minimum_age, maximum_age),
        "cohorts": [],
        "notes": [],
        "instanceType": "StudyDesignPopulation",
    }

    characteristics = []
    if (allocation or "").strip().lower().startswith("random"):
        characteristics.append(_cdisc_code(ids, *CHARACTERISTIC_RANDOMISED))

    if not intervention_model:
        synthesized.append(
            "InterventionalStudyDesign.model = 'NOT STATED' "
            "(required by USDM; the source record does not state it)"
        )
    observational = (study_type or "").strip().upper().startswith("OBSERVATIONAL")
    design = {
        "id": ids.mint("StudyDesign"),
        "extensionAttributes": [],
        "name": "SD1",
        "label": "",
        "description": "",
        "studyType": _ctgov_code(ids, study_type) if study_type else None,
        "studyPhase": None,
        "therapeuticAreas": [],
        "characteristics": characteristics,
        "encounters": [],
        "activities": [],
        "arms": arms,
        "studyCells": [],
        "rationale": "",
        "epochs": [],
        "elements": [],
        "estimands": [],
        "indications": [],
        "studyInterventionIds": [],
        "objectives": projection.objectives,
        "population": population,
        "scheduleTimelines": [],
        "biospecimenRetentions": [],
        "documentVersionIds": [],
        "eligibilityCriteria": [],
        "analysisPopulations": projection.analysis_populations,
        "notes": [],
        "subTypes": [_ctgov_code(ids, primary_purpose)] if primary_purpose else [],
        "model": _ctgov_code(ids, intervention_model or "NOT STATED"),
        "instanceType": "ObservationalStudyDesign" if observational else "InterventionalStudyDesign",
    }
    if observational:
        design["timePerspective"] = _ctgov_code(ids, "NOT STATED")
        design["samplingMethod"] = None
        synthesized.append(
            "ObservationalStudyDesign.timePerspective = 'NOT STATED' (required by USDM)"
        )
    else:
        design["intentTypes"] = []
        design["blindingSchema"] = None
        if masking:
            design["extensionAttributes"].append(
                {
                    "id": ids.mint("ExtensionAttribute"),
                    "url": f"{codes.EXTENSION_NS}:masking",
                    "valueString": masking,
                    "instanceType": "ExtensionAttribute",
                }
            )

    synthesized.extend(
        [
            "StudyVersion.rationale = '' (required by USDM; a registry record has no protocol rationale)",
            "StudyDesign.rationale = '' (same)",
            "StudyDesign.studyCells / epochs / eligibilityCriteria = [] (not collected)",
        ]
    )

    study_version = {
        "id": ids.mint("StudyVersion"),
        "extensionAttributes": [],
        "versionIdentifier": "1",
        "rationale": "",
        "documentVersionIds": [],
        "dateValues": [],
        "amendments": [],
        "businessTherapeuticAreas": [],
        "studyIdentifiers": [
            {
                "id": ids.mint("StudyIdentifier"),
                "extensionAttributes": [],
                "text": projection.nct_id,
                "scopeId": organization["id"],
                "instanceType": "StudyIdentifier",
            }
        ],
        "referenceIdentifiers": [],
        "studyDesigns": [design],
        "titles": titles,
        "eligibilityCriterionItems": [],
        "narrativeContentItems": [],
        "abbreviations": [],
        "roles": [],
        "organizations": [organization],
        "studyInterventions": [],
        "administrableProducts": [],
        "medicalDevices": [],
        "productOrganizationRoles": [],
        "biomedicalConcepts": [],
        "bcCategories": [],
        "bcSurrogates": projection.bc_surrogates,
        "dictionaries": projection.dictionaries,
        "conditions": [],
        "notes": [
            {
                "id": ids.mint("CommentAnnotation"),
                "extensionAttributes": [],
                "text": (
                    "Projected from ClinicalTrials.gov registry data by "
                    f"{codes.SYSTEM_NAME}. Attributes required by USDM but absent from the "
                    "source record: " + "; ".join(synthesized)
                ),
                "codes": [],
                "instanceType": "CommentAnnotation",
            }
        ],
        "instanceType": "StudyVersion",
    }

    return {
        "study": {
            "id": projection.study_id,
            "extensionAttributes": [],
            "name": projection.nct_id,
            "description": brief_title or "",
            "label": brief_title or "",
            "versions": [study_version],
            "documentedBy": [],
            "instanceType": "Study",
        },
        "usdmVersion": codes.USDM_VERSION,
        "systemName": codes.SYSTEM_NAME,
        "systemVersion": "",
        "provenance": {
            **provenance(con, projection, vocab_version=vocab_version),
            "synthesized": synthesized,
        },
    }


def _age_range(ids: IdFactory, minimum_age: str | None, maximum_age: str | None) -> dict | None:
    """CT.gov ages are "18 Years" / "N/A"; USDM wants a Range of Quantity.

    The unit is left null, as the CDISC pilot does for enrolment, and the source
    strings travel verbatim in extensions rather than being mapped onto a unit
    codelist this projection cannot verify.
    """
    low, high = _age_value(minimum_age), _age_value(maximum_age)
    if low is None and high is None:
        return None
    return {
        "id": ids.mint("Range"),
        "extensionAttributes": [
            {
                "id": ids.mint("ExtensionAttribute"),
                "url": f"{codes.EXTENSION_NS}:ageRangeSource",
                "valueString": f"{minimum_age or ''}..{maximum_age or ''}",
                "instanceType": "ExtensionAttribute",
            }
        ],
        "minValue": _quantity(ids, low if low is not None else 0),
        "maxValue": _quantity(ids, high if high is not None else 0),
        "isApproximate": False,
        "instanceType": "Range",
    }


def _age_value(raw: str | None) -> float | None:
    if not raw:
        return None
    token = str(raw).strip().split()
    try:
        return float(token[0])
    except (IndexError, ValueError):
        return None
