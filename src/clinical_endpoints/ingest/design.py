"""Study-level design and eligibility facts, shared by both ingestion backends.

The eight columns `raw.studies` started with were what the conforming pipeline
needed. The USDM projection needs more: a schema-valid USDM `Wrapper` requires
`StudyDesignPopulation.includesHealthySubjects` and
`InterventionalStudyDesign.model`, and neither was collected. Filling them with
placeholders would put invented clinical facts into a standards-conformant
document, so they are sourced instead.

The mapping is CDISC's own, not this project's invention:
DDF-RA/Documents/Mappings/ct-gov_mapping.xlsx maps ClinicalTrials.gov fields to
USDM 4.0.0 paths. The rows used here:

| CT.gov field                | USDM target                                  |
|-----------------------------|----------------------------------------------|
| Interventional Study Model  | InterventionalStudyDesign.model              |
| Primary Purpose             | InterventionalStudyDesign.subTypes           |
| Allocation                  | StudyDesign.characteristics ("Randomized")   |
| Masking                     | StudyRole.code / Masking.text                |
| Enrollment                  | StudyDesignPopulation.plannedEnrollmentNumber|
| Accepts Healthy Volunteers  | StudyDesignPopulation.includesHealthySubjects|
| Sex                         | StudyDesignPopulation.plannedSex             |
| Minimum / Maximum Age       | StudyDesignPopulation.plannedAge             |
| Study Population Description| StudyDesignPopulation.description            |
| Arm Title / Type            | StudyArm.label / StudyArm.type               |

Both backends write the same shape, so everything downstream stays
source-agnostic -- the property the README calls out as load-bearing.
"""

from __future__ import annotations

from typing import Any, Optional

#: Appended to the original eight columns of raw.studies. Additive: existing
#: columns keep their names and meanings.
STUDY_DESIGN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("intervention_model", "VARCHAR"),
    ("primary_purpose", "VARCHAR"),
    ("allocation", "VARCHAR"),
    ("masking", "VARCHAR"),
    ("enrollment_count", "INTEGER"),
    ("enrollment_type", "VARCHAR"),
    ("healthy_volunteers", "BOOLEAN"),
    ("gender", "VARCHAR"),
    ("minimum_age", "VARCHAR"),
    ("maximum_age", "VARCHAR"),
    ("population_description", "VARCHAR"),
)

STUDY_BASE_COLUMNS: tuple[str, ...] = (
    "nct_id", "phase", "overall_status", "study_type",
    "start_date", "primary_completion_date", "brief_title", "official_title",
)

STUDY_COLUMNS: tuple[str, ...] = STUDY_BASE_COLUMNS + tuple(c for c, _ in STUDY_DESIGN_COLUMNS)

STUDIES_DDL = """
    nct_id VARCHAR PRIMARY KEY, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
    start_date DATE, primary_completion_date DATE,
    brief_title VARCHAR, official_title VARCHAR,
""" + ",\n    ".join(f"{name} {sql_type}" for name, sql_type in STUDY_DESIGN_COLUMNS)

DESIGN_GROUPS_DDL = "nct_id VARCHAR, group_type VARCHAR, title VARCHAR, description VARCHAR"
DESIGN_GROUPS_COLUMNS: tuple[str, ...] = ("nct_id", "group_type", "title", "description")

#: AACT stores this as free text ("Accepts Healthy Volunteers" / "No"); the
#: CT.gov API as a JSON boolean. Both normalise here so `raw.studies` carries one
#: type, and an unrecognised value stays NULL rather than guessing -- a wrong
#: `includesHealthySubjects` is a clinical claim, not a formatting slip.
_HEALTHY_VOLUNTEER_TRUE = frozenset({"accepts healthy volunteers", "yes", "true", "y"})
_HEALTHY_VOLUNTEER_FALSE = frozenset({"no", "false", "n"})


def normalise_healthy_volunteers(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in _HEALTHY_VOLUNTEER_TRUE:
        return True
    if token in _HEALTHY_VOLUNTEER_FALSE:
        return False
    return None


def normalise_enrollment_count(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
