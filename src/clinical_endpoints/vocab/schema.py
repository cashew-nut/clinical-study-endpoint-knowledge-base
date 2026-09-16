"""Declarative description of the vocab/*.yaml files. Adding a dimension is a
table edit here plus a new vocab file."""

from __future__ import annotations

from dataclasses import dataclass, field

VOCAB_DIRNAME = "vocab"


@dataclass(frozen=True)
class DimensionSpec:
    filename: str
    dimension: str
    table: str
    columns: tuple[str, ...]
    # Term fields whose values must be ids in another dimension.
    references: dict[str, str] = field(default_factory=dict)
    # Term fields holding a list of ids in another dimension.
    list_references: dict[str, str] = field(default_factory=dict)


DIMENSIONS: tuple[DimensionSpec, ...] = (
    DimensionSpec(
        filename="forms.yaml",
        dimension="form",
        table="forms",
        columns=(
            "id", "label", "definition", "direction_rule", "analysable", "expects_threshold",
            "event_family", "reference_entailed", "notes",
        ),
        list_references={"typical_reference": "reference", "typical_scale": "scale"},
    ),
    DimensionSpec(
        filename="measurements.yaml",
        dimension="measurement",
        table="measurements",
        columns=(
            "id", "label", "inline_label", "definition", "concept", "method", "domain",
            "default_direction", "event_polarity", "default_scale", "composite", "mcid",
            "implies_event", "notes",
        ),
        references={"default_direction": "direction", "default_scale": "scale", "implies_event": "event"},
        list_references={"typical_ta": "therapeutic_area"},
    ),
    DimensionSpec(
        filename="references.yaml",
        dimension="reference",
        table="references",
        columns=("id", "label", "inline_label", "definition", "kind", "notes"),
        list_references={"implies_form": "form"},
    ),
    DimensionSpec(
        filename="events.yaml",
        dimension="event",
        table="events",
        columns=("id", "label", "inline_label", "definition", "concept", "polarity", "notes"),
        list_references={"ascertained_by": "measurement", "components": "event"},
    ),
    DimensionSpec(
        filename="directions.yaml",
        dimension="direction",
        table="directions",
        columns=("id", "label", "definition", "sign", "notes"),
        list_references={"applies_to_forms": "form"},
    ),
    DimensionSpec(
        filename="scales.yaml",
        dimension="scale",
        table="scales",
        columns=("id", "label", "inline_label", "definition", "kind", "si_equivalent", "factor_to_si", "notes"),
    ),
    DimensionSpec(
        filename="therapeutic_areas.yaml",
        dimension="therapeutic_area",
        table="therapeutic_areas",
        columns=("id", "label", "definition", "precedence", "notes"),
    ),
    DimensionSpec(
        filename="drug_classes.yaml",
        dimension="drug_class",
        table="drug_classes",
        columns=("id", "label", "inline_label", "definition", "kind", "parent", "precedence", "notes"),
        references={"parent": "drug_class"},
    ),
    DimensionSpec(
        filename="timepoint_patterns.yaml",
        dimension="timepoint_pattern",
        table="timepoint_patterns",
        columns=("id", "label", "definition", "priority", "role", "notes"),
    ),
)

# The files below are not term lists and are loaded separately.
MAPPING_FILENAME = "ta_mesh_mapping.yaml"
DRUG_CLASS_MAPPING_FILENAME = "drug_class_mesh_mapping.yaml"
MATCHING_FILENAME = "matching.yaml"
USDM_TEMPLATES_FILENAME = "usdm_templates.yaml"
NAMED_ENDPOINTS_FILENAME = "named_endpoints.yaml"

ALL_FILENAMES: tuple[str, ...] = (
    tuple(d.filename for d in DIMENSIONS)
    + (MAPPING_FILENAME, DRUG_CLASS_MAPPING_FILENAME, MATCHING_FILENAME,
       USDM_TEMPLATES_FILENAME, NAMED_ENDPOINTS_FILENAME)
)

DRUG_CLASS_KINDS = frozenset({"mechanism", "pharmacologic", "modality", "control"})


def normalise_intervention_type(value: str | None) -> str | None:
    """Fold an intervention_type for `modality_rules` lookup. Used by both the
    loader and the resolver, since the backends spell these differently
    (DIETARY_SUPPLEMENT vs "Dietary Supplement")."""
    if not value:
        return None
    folded = value.replace("_", " ").replace("-", " ")
    return " ".join(folded.strip().lower().split()) or None


MATCH_METHODS = frozenset({"exact", "syntactic_rule", "semantic", "named_endpoint"})
CASCADE_FIELDS = frozenset({"measure", "description", "time_frame"})

DIRECTION_RULES = frozenset(
    {"inherit_measurement", "higher_count_better", "inherit_event_polarity", "time_polarity", "neutral"}
)
MEASUREMENT_DOMAINS = frozenset(
    {
        "efficacy", "safety", "pharmacokinetic", "pharmacodynamic", "immunogenicity",
        "patient_reported", "healthcare_utilisation", "exploratory",
    }
)
EVENT_POLARITIES = frozenset({"harm", "benefit"})
REFERENCE_KINDS = frozenset({"time_origin", "value_reference", "external_standard"})

TIMEPOINT_ROLES = frozenset({"assessment_time", "observation_window", "event_horizon", "unresolved"})

# The `derived` flag values the projection may emit.
DERIVED_ATTRIBUTES = frozenset({"purpose", "reference", "objective"})

# The closed set of tags a USDM syntax template may use; each needs a USDM
# home for its value (see usdm/tags.py). The analysis population is not a tag:
# it is projected as an AnalysisPopulation linked from the decomposition rather
# than rendered into the text.
USDM_TAGS = frozenset({"measurement", "concept", "reference", "timepoint", "threshold", "scale", "event"})

USDM_OBJECTIVE_KEYS = frozenset({"primary", "secondary", "exploratory", "_unresolved"})
