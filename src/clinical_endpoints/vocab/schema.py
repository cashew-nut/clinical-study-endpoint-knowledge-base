"""Declarative description of the vocab/*.yaml files.

Kept separate from the loader so that "what a vocab file may contain" is
readable in one place, and so adding a dimension is a table edit rather than a
code change (plan section 4: adding a dimension is a new vocab file + a new
column, not a redesign).
"""

from __future__ import annotations

from dataclasses import dataclass, field

VOCAB_DIRNAME = "vocab"


@dataclass(frozen=True)
class DimensionSpec:
    """One vocab file: its filename, the `dimension` it must declare, and the
    per-term scalar fields that become columns on its `vocab.<table>` table."""

    filename: str
    dimension: str
    table: str
    columns: tuple[str, ...]
    # Term fields whose values must be ids in another dimension.
    references: dict[str, str] = field(default_factory=dict)
    # Term fields holding a LIST of ids in another dimension.
    list_references: dict[str, str] = field(default_factory=dict)


DIMENSIONS: tuple[DimensionSpec, ...] = (
    DimensionSpec(
        filename="forms.yaml",
        dimension="form",
        table="forms",
        columns=("id", "label", "definition", "direction_rule", "analysable", "expects_threshold", "notes"),
        list_references={"typical_reference": "reference", "typical_scale": "scale"},
    ),
    DimensionSpec(
        filename="measurements.yaml",
        dimension="measurement",
        table="measurements",
        columns=(
            "id", "label", "definition", "concept", "method", "domain",
            "default_direction", "event_polarity", "default_scale", "composite", "mcid", "notes",
        ),
        references={"default_direction": "direction", "default_scale": "scale"},
        list_references={"typical_ta": "therapeutic_area"},
    ),
    DimensionSpec(
        filename="references.yaml",
        dimension="reference",
        table="references",
        columns=("id", "label", "definition", "kind", "notes"),
        list_references={"implies_form": "form"},
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
        columns=("id", "label", "definition", "kind", "si_equivalent", "factor_to_si", "notes"),
    ),
    DimensionSpec(
        filename="therapeutic_areas.yaml",
        dimension="therapeutic_area",
        table="therapeutic_areas",
        columns=("id", "label", "definition", "precedence", "notes"),
    ),
    DimensionSpec(
        filename="timepoint_patterns.yaml",
        dimension="timepoint_pattern",
        table="timepoint_patterns",
        columns=("id", "label", "definition", "priority", "notes"),
    ),
)

# ta_mesh_mapping.yaml is not a term list, so it is loaded separately.
MAPPING_FILENAME = "ta_mesh_mapping.yaml"

# matching.yaml is not a term list either: it is the contract for HOW the term
# files are matched (whole-token synonyms, case-sensitive short acronyms,
# longest-match where a file declares no precedence, and which field to read when
# the first one is silent). It is loaded and validated rather than left as
# documentation, because the rules it states are load-bearing -- matching
# synonyms as substrings instead of whole tokens assigned 9.8% of the corpus to
# one wrong measurement while making coverage look 23 points better.
MATCHING_FILENAME = "matching.yaml"

ALL_FILENAMES: tuple[str, ...] = (
    tuple(d.filename for d in DIMENSIONS) + (MAPPING_FILENAME, MATCHING_FILENAME)
)

# Closed value sets for matching.yaml.
MATCH_METHODS = frozenset({"exact", "syntactic_rule", "semantic"})
CASCADE_FIELDS = frozenset({"measure", "description", "time_frame"})

# Closed value sets checked during validation.
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
