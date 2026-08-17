"""Layer C: project Layer A and B into CDISC USDM v4.

The projection emits the classes the exchange format actually needs for endpoints:
Objective, Endpoint, SyntaxTemplateDictionary with ParameterMaps, Activity, Timing and
Estimand, wrapped in the Study / StudyVersion / StudyDesign envelope.

Two properties are load-bearing.

*Codes are real.* Endpoint.level and Objective.level carry the NCI C-codes from the
CDISC DDF controlled terminology, not invented ones, so a consuming system resolves
them against the same codelists it already uses.

*Parameters stay parameters.* An endpoint's text is a SyntaxTemplate carrying `[Tag]`
placeholders, and each tag resolves through a ParameterMap to a `<usdm:ref>` pointing
at the object that supplies the value. That is what makes the projection a structured
document rather than a sentence: a renderer can produce prose, and an analysis system
can read the Timing and Activity objects directly.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import duckdb

from ceskb.config import DERIVATION_VERSION
from ceskb.store.db import _json, utcnow
from ceskb.vocab.loader import Concept, Vocabulary, load_vocabulary

USDM_VERSION = "4.0.0"
CODE_SYSTEM = "http://www.cdisc.org"
CODE_SYSTEM_VERSION = "2025-06-03"

#: NCI C-codes from CDISC DDF-RA Deliverables/CT/USDM_CT.xlsx.
#: Endpoint.level codelist C188726, Objective.level codelist C188725.
ENDPOINT_LEVEL_CODES: dict[str, tuple[str, str]] = {
    "primary": ("C94496", "Primary Endpoint"),
    "secondary": ("C139173", "Secondary Endpoint"),
    "exploratory": ("C170559", "Exploratory Endpoint"),
}
OBJECTIVE_LEVEL_CODES: dict[str, tuple[str, str]] = {
    "primary": ("C85826", "Primary Objective"),
    "secondary": ("C85827", "Secondary Objective"),
    "exploratory": ("C163559", "Exploratory Objective"),
}
#: Timing.type codelist C201264, Timing.relativeToFrom codelist C201265.
TIMING_TYPE_CODES: dict[str, tuple[str, str]] = {
    "fixed_reference": ("C201358", "Fixed Reference"),
    "after": ("C201356", "After"),
    "before": ("C201357", "Before"),
}
TIMING_RELATIVE_CODES: dict[str, tuple[str, str]] = {
    "start_to_start": ("C201355", "Start to Start"),
    "start_to_end": ("C201354", "Start to End"),
    "end_to_start": ("C201353", "End to Start"),
    "end_to_end": ("C201352", "End to End"),
}

#: ISO 8601 duration designators for the offset units the extractors produce.
_ISO_DURATION: dict[str, str] = {
    "day": "P{n}D",
    "week": "P{n}W",
    "month": "P{n}M",
    "year": "P{n}Y",
}


class ProjectionError(Exception):
    """Raised when a specification cannot be projected."""


def _id(*parts: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, "ceskb:usdm:" + ":".join(parts)).hex


def _code(identifier: str, code: str, decode: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "code": code,
        "codeSystem": CODE_SYSTEM,
        "codeSystemVersion": CODE_SYSTEM_VERSION,
        "decode": decode,
        "instanceType": "Code",
    }


def _ref(klass: str, identifier: str, attribute: str) -> str:
    """A USDM parameter reference, in the form the DDF tooling emits and parses."""
    return f'<usdm:ref klass="{klass}" id="{identifier}" attribute="{attribute}"></usdm:ref>'


@dataclass
class SpecRow:
    """A Layer B specification, flattened for projection."""

    spec_id: str
    study_id: str
    outcome_uid: str
    concept_id: str
    endpoint_level: str
    measure: str
    description: str
    time_frame: str
    timepoint_anchor: str | None
    timepoint_selection: str | None
    timepoint_value: float | None
    timepoint_unit: str | None
    timepoint_raw: str | None
    threshold_kind: str | None
    threshold_operator: str | None
    threshold_value: float | None
    threshold_unit: str | None
    analysis_population: str | None
    direction: str | None
    summary_measure: str | None
    unresolved_axes: list[str] = field(default_factory=list)


def _iso_duration(value: float | None, unit: str | None) -> str | None:
    if value is None or unit not in _ISO_DURATION:
        return None
    number = int(value) if float(value).is_integer() else value
    return _ISO_DURATION[unit].format(n=number)


def _anchor_timing_type(anchor: str | None) -> tuple[str, str]:
    """Map a timepoint anchor onto the USDM Timing.type codelist.

    Every anchor this project recognises is a point the assessment follows, so the
    mapping is to 'After' except where no anchor was resolved, which becomes a fixed
    reference so the document still carries the offset it does know.
    """
    if anchor in (None, "unspecified", "event_driven"):
        return TIMING_TYPE_CODES["fixed_reference"]
    return TIMING_TYPE_CODES["after"]


class UsdmProjector:
    """Builds a USDM document for one study from its endpoint specifications."""

    def __init__(self, vocab: Vocabulary | None = None) -> None:
        self.vocab = vocab or load_vocabulary()

    # ------------------------------------------------------------------ #
    # template construction
    # ------------------------------------------------------------------ #
    def _endpoint_template(self, spec: SpecRow, concept: Concept) -> str:
        """Produce the endpoint's SyntaxTemplate text.

        Uses the concept's authored template where there is one, and otherwise builds a
        parameterised sentence from the structure so that every endpoint -- including
        ones nobody has written a template for -- still projects with real tags rather
        than with the raw registry title.
        """
        authored = concept.usdm_hints.get("endpoint_text_template")
        if authored:
            return authored

        pieces = [concept.label]
        if spec.threshold_value is not None and spec.threshold_kind not in (
            None,
            "event_occurrence",
        ):
            pieces.append("meeting [Threshold]")
        if spec.timepoint_value is not None:
            pieces.append("at [Timepoint]")
        if spec.analysis_population and spec.analysis_population != "unspecified":
            pieces.append("in the [Population]")
        return " ".join(pieces) + "."

    def _objective_template(self, concept: Concept) -> str:
        authored = concept.usdm_hints.get("objective_text_template")
        if authored:
            return authored
        return f"To evaluate [EndpointRef] in the study population."

    # ------------------------------------------------------------------ #
    # component builders
    # ------------------------------------------------------------------ #
    def _timing(self, spec: SpecRow) -> dict[str, Any] | None:
        duration = _iso_duration(spec.timepoint_value, spec.timepoint_unit)
        if duration is None and spec.timepoint_anchor in (None, "unspecified"):
            return None
        timing_id = _id("timing", spec.spec_id)
        type_code, type_decode = _anchor_timing_type(spec.timepoint_anchor)
        rel_code, rel_decode = TIMING_RELATIVE_CODES["start_to_start"]
        anchor_label = (spec.timepoint_anchor or "unspecified").replace("_", " ")
        return {
            "id": timing_id,
            "name": f"TIMING_{spec.outcome_uid}",
            "label": spec.timepoint_raw or anchor_label,
            "description": (
                f"Assessment timing derived from the registry time frame "
                f"{spec.time_frame!r}." if spec.time_frame else "Assessment timing."
            ),
            "type": _code(_id("timing-type", spec.spec_id), type_code, type_decode),
            "value": duration or "P0D",
            "valueLabel": spec.timepoint_raw or (duration or "not stated"),
            "relativeToFrom": _code(_id("timing-rel", spec.spec_id), rel_code, rel_decode),
            "relativeFromScheduledInstanceId": _id("anchor", spec.study_id, anchor_label),
            "relativeToScheduledInstanceId": None,
            "windowLower": None,
            "windowUpper": None,
            "windowLabel": None,
            "instanceType": "Timing",
        }

    def _activities(self, spec: SpecRow, concept: Concept) -> list[dict[str, Any]]:
        labels = concept.usdm_hints.get("suggested_activity_labels") or []
        if not labels:
            measurement = concept.structure.get("measurement_concept", "assessment")
            term = self.vocab.axis("measurement_concept").terms.get(measurement)
            labels = [term.label if term else measurement.replace("_", " ").title()]
        activities = []
        for index, label in enumerate(labels):
            activities.append(
                {
                    "id": _id("activity", spec.study_id, concept.concept_id, str(index)),
                    "name": re.sub(r"[^A-Za-z0-9]+", "_", label).upper()[:60],
                    "label": label,
                    "description": (
                        f"Activity supplying the measurement for {concept.label}."
                    ),
                    "previousId": None,
                    "nextId": None,
                    "childIds": [],
                    "definedProcedures": [],
                    "biomedicalConceptIds": [],
                    "bcCategoryIds": [],
                    "bcSurrogateIds": [],
                    "timelineId": None,
                    "notes": [],
                    "instanceType": "Activity",
                }
            )
        return activities

    def _dictionary(
        self,
        spec: SpecRow,
        concept: Concept,
        template_text: str,
        timing: dict[str, Any] | None,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the parameter map for the tags actually present in the template."""
        tags = re.findall(r"\[([^\]]+)\]", template_text)
        parameter_maps: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(tag: str, reference: str) -> None:
            if tag in seen:
                return
            seen.add(tag)
            parameter_maps.append(
                {
                    "id": _id("pmap", spec.spec_id, tag),
                    "tag": tag,
                    "reference": reference,
                    "instanceType": "ParameterMap",
                }
            )

        for tag in tags:
            if tag == "Timepoint" and timing:
                add(tag, _ref("Timing", timing["id"], "valueLabel"))
            elif tag in {"Assessor", "Criteria"} and activities:
                add(tag, _ref("Activity", activities[0]["id"], "label"))
            elif tag == "BaselineActivity" and activities:
                add(tag, _ref("Activity", activities[0]["id"], "label"))
            elif tag == "Threshold":
                threshold_text = self._threshold_text(spec)
                add(tag, f'<usdm:tag name="Threshold">{threshold_text}</usdm:tag>')
            elif tag == "Population":
                population = (spec.analysis_population or "unspecified").replace("_", " ")
                add(tag, f'<usdm:tag name="Population">{population}</usdm:tag>')
            elif tag == "TimeOrigin":
                anchor = (spec.timepoint_anchor or "unspecified").replace("_", " ")
                add(tag, f'<usdm:tag name="TimeOrigin">{anchor}</usdm:tag>')
            else:
                # A tag with no resolvable target still gets a map, marked unresolved,
                # so a consumer sees an explicit gap instead of a dangling placeholder.
                add(tag, f'<usdm:tag name="{tag}">unresolved</usdm:tag>')

        return {
            "id": _id("dict", spec.spec_id),
            "name": f"DICT_{spec.outcome_uid}",
            "label": f"Parameter dictionary for {concept.label}",
            "description": (
                "Resolves the parameter tags used by this endpoint's syntax template."
            ),
            "parameterMaps": parameter_maps,
            "instanceType": "SyntaxTemplateDictionary",
        }

    def _threshold_text(self, spec: SpecRow) -> str:
        if spec.threshold_value is None:
            return "as defined by the criteria"
        operator_axis = self.vocab.axis("threshold_operator")
        term = operator_axis.terms.get(spec.threshold_operator or "")
        symbol = (term.attributes.get("symbol") if term else None) or ""
        unit_label = ""
        if spec.threshold_unit:
            unit_term = self.vocab.axis("unit").terms.get(spec.threshold_unit)
            unit_label = f" {unit_term.label}" if unit_term else f" {spec.threshold_unit}"
        value = spec.threshold_value
        rendered = int(value) if float(value).is_integer() else value
        return f"{symbol} {rendered}{unit_label}".strip()

    def _estimand(
        self, spec: SpecRow, concept: Concept, endpoint_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build an Estimand and its AnalysisPopulation.

        Registry records never state intercurrent event handling, so the projection
        emits a single IntercurrentEvent with strategy 'unspecified' rather than
        asserting a strategy nobody chose. That keeps the estimand structurally
        complete and factually honest at the same time.
        """
        population_term = spec.analysis_population or "unspecified"
        population_axis = self.vocab.axis("analysis_population")
        population_label = (
            population_axis.terms[population_term].label
            if population_term in population_axis.terms
            else "Unspecified"
        )
        population_id = _id("pop", spec.study_id, population_term)
        analysis_population = {
            "id": population_id,
            "name": f"POP_{population_term.upper()}",
            "label": population_label,
            "description": (
                population_axis.terms[population_term].definition
                if population_term in population_axis.terms
                else "The analysis population was not stated in the source record."
            ),
            "text": population_label,
            "subsetOfIds": [],
            "notes": [],
            "instanceType": "AnalysisPopulation",
        }

        summary_term = spec.summary_measure or "unspecified"
        summary_axis = self.vocab.axis("summary_measure")
        summary_label = (
            summary_axis.terms[summary_term].label
            if summary_term in summary_axis.terms
            else "Unspecified"
        )

        estimand = {
            "id": _id("estimand", spec.spec_id),
            "name": f"ESTIMAND_{spec.outcome_uid}",
            "label": f"Estimand for {concept.label}",
            "description": (
                "Assembled from the registry record. Intercurrent event handling is not "
                "reported in registry outcome text, so it is recorded as unspecified "
                "rather than assumed."
            ),
            "populationSummary": summary_label,
            "analysisPopulationId": population_id,
            "interventionIds": [],
            "variableOfInterestId": endpoint_id,
            "intercurrentEvents": [
                {
                    "id": _id("ice", spec.spec_id),
                    "name": f"ICE_{spec.outcome_uid}",
                    "label": "Not stated in source",
                    "description": (
                        "No intercurrent event handling strategy is stated in the source "
                        "record."
                    ),
                    "text": "Not stated in source.",
                    "dictionaryId": None,
                    "strategy": "unspecified",
                    "notes": [],
                    "instanceType": "IntercurrentEvent",
                }
            ],
            "notes": [],
            "instanceType": "Estimand",
        }
        return estimand, analysis_population

    # ------------------------------------------------------------------ #
    # document assembly
    # ------------------------------------------------------------------ #
    def project_study(self, study: dict[str, Any], specs: Iterable[SpecRow]) -> dict[str, Any]:
        specs = list(specs)
        objectives_by_level: dict[str, dict[str, Any]] = {}
        dictionaries: list[dict[str, Any]] = []
        activities: list[dict[str, Any]] = []
        timings: list[dict[str, Any]] = []
        estimands: list[dict[str, Any]] = []
        populations: dict[str, dict[str, Any]] = {}

        for spec in specs:
            concept = self.vocab.concept(spec.concept_id)
            level = spec.endpoint_level
            endpoint_id = _id("endpoint", spec.spec_id)

            timing = self._timing(spec)
            if timing:
                timings.append(timing)
            spec_activities = self._activities(spec, concept)
            activities.extend(spec_activities)

            template_text = self._endpoint_template(spec, concept)
            dictionary = self._dictionary(spec, concept, template_text, timing, spec_activities)
            dictionaries.append(dictionary)

            endpoint_code, endpoint_decode = ENDPOINT_LEVEL_CODES.get(
                level, ENDPOINT_LEVEL_CODES["exploratory"]
            )
            endpoint = {
                "id": endpoint_id,
                "name": f"EP_{spec.outcome_uid}",
                "label": concept.label,
                "description": spec.measure or concept.label,
                "text": template_text,
                "dictionaryId": dictionary["id"],
                "purpose": (
                    f"Derived from registry outcome {spec.outcome_uid} and mapped to "
                    f"canonical concept {concept.concept_id}."
                ),
                "level": _code(_id("ep-level", spec.spec_id), endpoint_code, endpoint_decode),
                "notes": [],
                "instanceType": "Endpoint",
            }

            if level not in objectives_by_level:
                objective_code, objective_decode = OBJECTIVE_LEVEL_CODES.get(
                    level, OBJECTIVE_LEVEL_CODES["exploratory"]
                )
                objectives_by_level[level] = {
                    "id": _id("objective", spec.study_id, level),
                    "name": f"OBJ_{level.upper()}",
                    "label": f"{level.title()} objective",
                    "description": (
                        f"{level.title()} objective assembled from the study's "
                        f"{level} endpoints."
                    ),
                    "text": self._objective_template(concept),
                    "dictionaryId": None,
                    "level": _code(
                        _id("obj-level", spec.study_id, level), objective_code, objective_decode
                    ),
                    "endpoints": [],
                    "notes": [],
                    "instanceType": "Objective",
                }
            objectives_by_level[level]["endpoints"].append(endpoint)

            estimand, population = self._estimand(spec, concept, endpoint_id)
            estimands.append(estimand)
            populations[population["id"]] = population

        level_order = {"primary": 0, "secondary": 1, "exploratory": 2}
        objectives = sorted(
            objectives_by_level.values(),
            key=lambda o: level_order.get(o["name"].split("_")[1].lower(), 9),
        )

        study_design = {
            "id": _id("design", study["study_id"]),
            "name": "DESIGN_1",
            "label": study.get("brief_title") or study["study_id"],
            "description": "Study design assembled from registry metadata.",
            "objectives": objectives,
            "estimands": estimands,
            "analysisPopulations": list(populations.values()),
            "activities": activities,
            "scheduleTimelines": [
                {
                    "id": _id("timeline", study["study_id"]),
                    "name": "TIMELINE_MAIN",
                    "label": "Main timeline",
                    "timings": timings,
                    "instanceType": "ScheduleTimeline",
                }
            ],
            "dictionaries": dictionaries,
            "instanceType": "InterventionalStudyDesign",
        }

        study_version = {
            "id": _id("version", study["study_id"]),
            "versionIdentifier": "1",
            "rationale": (
                "Projected from public registry data by the clinical study endpoint "
                "knowledge base."
            ),
            "titles": [
                {
                    "id": _id("title", study["study_id"]),
                    "text": study.get("official_title") or study.get("brief_title") or "",
                    "instanceType": "StudyTitle",
                }
            ],
            "studyIdentifiers": [
                {
                    "id": _id("identifier", study["study_id"]),
                    "text": study["study_id"],
                    "instanceType": "StudyIdentifier",
                }
            ],
            "studyDesigns": [study_design],
            "instanceType": "StudyVersion",
        }

        return {
            "study": {
                "id": _id("study", study["study_id"]),
                "name": study["study_id"],
                "label": study.get("brief_title"),
                "description": study.get("official_title"),
                "versions": [study_version],
                "documentedBy": [],
                "instanceType": "Study",
            },
            "usdmVersion": USDM_VERSION,
            "systemName": "ceskb",
            "systemVersion": DERIVATION_VERSION,
            "_provenance": {
                "generated": date.today().isoformat(),
                "source": "ClinicalTrials.gov registry record, projected via ceskb",
                "derivation_version": DERIVATION_VERSION,
                "endpoint_count": len(specs),
                "unresolved": sorted(
                    {axis for spec in specs for axis in spec.unresolved_axes}
                ),
                "caveat": (
                    "Registry records do not state intercurrent event strategies, "
                    "analysis populations for every endpoint, or assessment anchors. "
                    "Those attributes are marked unspecified rather than inferred."
                ),
            },
        }


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def _load_specs(conn: duckdb.DuckDBPyConnection, study_id: str) -> list[SpecRow]:
    rows = conn.execute(
        """
        SELECT e.spec_id, e.study_id, e.outcome_uid, e.concept_id, e.endpoint_level,
               coalesce(o.measure, ''), coalesce(o.description, ''), coalesce(o.time_frame, ''),
               e.timepoint_anchor, e.timepoint_selection, e.timepoint_value,
               e.timepoint_unit, e.timepoint_raw,
               e.threshold_kind, e.threshold_operator, e.threshold_value, e.threshold_unit,
               e.analysis_population, e.direction, e.summary_measure,
               coalesce(e.unresolved_axes, '[]')
        FROM endpoint_spec e
        JOIN study_outcome o USING (outcome_uid)
        WHERE e.study_id = ?
        ORDER BY CASE e.endpoint_level
                     WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 ELSE 2 END,
                 e.outcome_uid
        """,
        [study_id],
    ).fetchall()
    return [
        SpecRow(
            spec_id=r[0], study_id=r[1], outcome_uid=r[2], concept_id=r[3], endpoint_level=r[4],
            measure=r[5], description=r[6], time_frame=r[7],
            timepoint_anchor=r[8], timepoint_selection=r[9], timepoint_value=r[10],
            timepoint_unit=r[11], timepoint_raw=r[12],
            threshold_kind=r[13], threshold_operator=r[14], threshold_value=r[15],
            threshold_unit=r[16], analysis_population=r[17], direction=r[18],
            summary_measure=r[19], unresolved_axes=json.loads(r[20]) or [],
        )
        for r in rows
    ]


def project_all(
    conn: duckdb.DuckDBPyConnection, vocab: Vocabulary | None = None
) -> dict[str, Any]:
    """Project every study that has at least one classified endpoint."""
    projector = UsdmProjector(vocab)
    study_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT study_id FROM endpoint_spec ORDER BY study_id"
        ).fetchall()
    ]
    conn.execute("DELETE FROM usdm_projection")

    written = 0
    for study_id in study_ids:
        row = conn.execute(
            "SELECT study_id, brief_title, official_title FROM study WHERE study_id = ?",
            [study_id],
        ).fetchone()
        if not row:
            continue
        study = {"study_id": row[0], "brief_title": row[1], "official_title": row[2]}
        specs = _load_specs(conn, study_id)
        document = projector.project_study(study, specs)
        conn.execute(
            """
            INSERT INTO usdm_projection (
                projection_id, study_id, usdm_version, generated_at,
                derivation_version, endpoint_count, document
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                _id("projection", study_id, DERIVATION_VERSION),
                study_id,
                USDM_VERSION,
                utcnow(),
                DERIVATION_VERSION,
                len(specs),
                _json(document),
            ],
        )
        written += 1

    return {"studies_projected": written, "usdm_version": USDM_VERSION}
