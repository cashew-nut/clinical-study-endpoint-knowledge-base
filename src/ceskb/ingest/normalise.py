"""Normalise raw registry records into study and outcome rows.

Every field path the registry exposes is declared once, here, in FIELD_PATHS. When the
upstream schema shifts, this is the only file that needs to change, and
`probe_schema` reports which declared paths were actually present in a batch so drift
surfaces as a number rather than as silently empty columns.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ceskb.vocab.loader import Vocabulary

#: Declared source field paths, dotted. Lists are traversed with [].
FIELD_PATHS: dict[str, str] = {
    "study_id": "protocolSection.identificationModule.nctId",
    "brief_title": "protocolSection.identificationModule.briefTitle",
    "official_title": "protocolSection.identificationModule.officialTitle",
    "overall_status": "protocolSection.statusModule.overallStatus",
    "start_date": "protocolSection.statusModule.startDateStruct.date",
    "primary_completion_date": "protocolSection.statusModule.primaryCompletionDateStruct.date",
    "completion_date": "protocolSection.statusModule.completionDateStruct.date",
    "last_update_posted": "protocolSection.statusModule.lastUpdatePostDateStruct.date",
    "lead_sponsor": "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
    "sponsor_class": "protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
    "conditions": "protocolSection.conditionsModule.conditions",
    "study_type": "protocolSection.designModule.studyType",
    "phases": "protocolSection.designModule.phases",
    "enrollment": "protocolSection.designModule.enrollmentInfo.count",
    "primary_outcomes": "protocolSection.outcomesModule.primaryOutcomes",
    "secondary_outcomes": "protocolSection.outcomesModule.secondaryOutcomes",
    "other_outcomes": "protocolSection.outcomesModule.otherOutcomes",
    "mesh_conditions": "derivedSection.conditionBrowseModule.meshes",
    "has_results": "hasResults",
}

#: Registry outcome collections mapped onto endpoint_level vocabulary terms.
OUTCOME_COLLECTIONS: dict[str, str] = {
    "primary_outcomes": "primary",
    "secondary_outcomes": "secondary",
    "other_outcomes": "exploratory",
}


def dig(record: Any, path: str) -> Any:
    """Walk a dotted path, returning None rather than raising on any miss."""
    current = record
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _hash(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def normalise_whitespace(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"\s+", " ", text).strip() or None


@dataclass
class NormalisedOutcome:
    outcome_uid: str
    study_id: str
    endpoint_level: str
    ordinal: int
    measure: str | None
    description: str | None
    time_frame: str | None
    record_hash: str


@dataclass
class NormalisedStudy:
    study_id: str
    source: str
    brief_title: str | None
    official_title: str | None
    overall_status: str | None
    study_type: str | None
    phases: list[str]
    enrollment: int | None
    lead_sponsor: str | None
    sponsor_class: str | None
    conditions: list[str]
    therapeutic_areas: list[str]
    ta_evidence: list[tuple[str, str, str]]
    start_date: str | None
    primary_completion_date: str | None
    completion_date: str | None
    last_update_posted: str | None
    has_results: bool | None
    record_hash: str
    outcomes: list[NormalisedOutcome] = field(default_factory=list)
    is_synthetic: bool = False


class TherapeuticAreaInferrer:
    """Assigns therapeutic areas by matching condition text against axis synonyms.

    Deliberately crude and fully transparent: every assignment records the term that
    matched and the field it matched in, so a wrong area is visible and fixable by
    editing one synonym list rather than by retraining anything.
    """

    def __init__(self, vocab: Vocabulary) -> None:
        axis = vocab.axis("therapeutic_area")
        self._patterns: list[tuple[str, str, re.Pattern[str]]] = []
        for term in axis.terms.values():
            if term.term_id == "cross_cutting":
                continue
            surface_forms = [term.label, *term.synonyms]
            for surface in surface_forms:
                self._patterns.append(
                    (
                        term.term_id,
                        surface,
                        re.compile(rf"\b{re.escape(surface.lower())}", re.IGNORECASE),
                    )
                )

    def infer(self, conditions: Iterable[str], mesh_terms: Iterable[str]) -> tuple[list[str], list[tuple[str, str, str]]]:
        haystacks = [("condition", " ; ".join(conditions).lower()), ("mesh", " ; ".join(mesh_terms).lower())]
        areas: dict[str, None] = {}
        evidence: list[tuple[str, str, str]] = []
        for term_id, surface, pattern in self._patterns:
            for source_field, haystack in haystacks:
                if haystack and pattern.search(haystack):
                    areas.setdefault(term_id, None)
                    evidence.append((term_id, surface, source_field))
                    break
        return list(areas), evidence


def _outcomes(record: dict[str, Any], study_id: str) -> list[NormalisedOutcome]:
    outcomes: list[NormalisedOutcome] = []
    for key, level in OUTCOME_COLLECTIONS.items():
        entries = dig(record, FIELD_PATHS[key]) or []
        if not isinstance(entries, list):
            continue
        for ordinal, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            measure = normalise_whitespace(entry.get("measure"))
            description = normalise_whitespace(entry.get("description"))
            time_frame = normalise_whitespace(entry.get("timeFrame"))
            if not (measure or description):
                continue
            outcomes.append(
                NormalisedOutcome(
                    outcome_uid=f"{study_id}:{level}:{ordinal}",
                    study_id=study_id,
                    endpoint_level=level,
                    ordinal=ordinal,
                    measure=measure,
                    description=description,
                    time_frame=time_frame,
                    record_hash=_hash(measure, description, time_frame),
                )
            )
    return outcomes


def normalise_study(
    record: dict[str, Any], source: str, ta_inferrer: TherapeuticAreaInferrer
) -> NormalisedStudy | None:
    """Map one raw record. Returns None when the record has no usable identifier."""
    study_id = dig(record, FIELD_PATHS["study_id"])
    if not study_id:
        return None

    conditions = dig(record, FIELD_PATHS["conditions"]) or []
    if not isinstance(conditions, list):
        conditions = []
    meshes = dig(record, FIELD_PATHS["mesh_conditions"]) or []
    mesh_terms = [m.get("term", "") for m in meshes if isinstance(m, dict)]

    areas, evidence = ta_inferrer.infer(conditions, mesh_terms)

    phases = dig(record, FIELD_PATHS["phases"]) or []
    if not isinstance(phases, list):
        phases = []

    enrollment = dig(record, FIELD_PATHS["enrollment"])
    if not isinstance(enrollment, int):
        enrollment = None

    outcomes = _outcomes(record, study_id)

    study = NormalisedStudy(
        study_id=study_id,
        source=source,
        brief_title=normalise_whitespace(dig(record, FIELD_PATHS["brief_title"])),
        official_title=normalise_whitespace(dig(record, FIELD_PATHS["official_title"])),
        overall_status=dig(record, FIELD_PATHS["overall_status"]),
        study_type=dig(record, FIELD_PATHS["study_type"]),
        phases=list(phases),
        enrollment=enrollment,
        lead_sponsor=normalise_whitespace(dig(record, FIELD_PATHS["lead_sponsor"])),
        sponsor_class=dig(record, FIELD_PATHS["sponsor_class"]),
        conditions=list(conditions),
        therapeutic_areas=areas,
        ta_evidence=evidence,
        start_date=dig(record, FIELD_PATHS["start_date"]),
        primary_completion_date=dig(record, FIELD_PATHS["primary_completion_date"]),
        completion_date=dig(record, FIELD_PATHS["completion_date"]),
        last_update_posted=dig(record, FIELD_PATHS["last_update_posted"]),
        has_results=dig(record, FIELD_PATHS["has_results"]),
        record_hash="",
        outcomes=outcomes,
        is_synthetic=bool(record.get("_synthetic")),
    )
    study.record_hash = _hash(
        study.brief_title,
        study.official_title,
        study.overall_status,
        study.last_update_posted,
        tuple(study.conditions),
        tuple(o.record_hash for o in outcomes),
    )
    return study


def probe_schema(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Report how often each declared field path resolved.

    Run against a live batch, this is what tells you whether the registry's schema has
    moved under you: a declared path whose present count is zero is either genuinely
    absent from that slice or no longer where the code expects it.
    """
    counts = {name: 0 for name in FIELD_PATHS}
    total = 0
    for record in records:
        total += 1
        for name, path in FIELD_PATHS.items():
            if dig(record, path) is not None:
                counts[name] += 1
    return {
        name: {
            "path": FIELD_PATHS[name],
            "present": counts[name],
            "total": total,
            "pct": round(100.0 * counts[name] / total, 1) if total else 0.0,
        }
        for name in FIELD_PATHS
    }
