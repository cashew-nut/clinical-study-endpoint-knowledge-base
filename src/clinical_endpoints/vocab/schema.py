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
        filename="timepoint_patterns.yaml",
        dimension="timepoint_pattern",
        table="timepoint_patterns",
        columns=("id", "label", "definition", "priority", "role", "notes"),
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

# usdm_templates.yaml is not a term list either: it is one syntax template per
# form id, plus the derived-text templates for the two USDM attributes a
# registry record never states (Endpoint.purpose, Objective.text). Loaded and
# validated for the same reason matching.yaml is -- the templates decide what
# every projected endpoint asserts, so a malformed one must fail the command
# rather than surface as a broken sentence in a standards-conformant document.
USDM_TEMPLATES_FILENAME = "usdm_templates.yaml"

# named_endpoints.yaml is not a dimension term list either (see that file's
# header): it states what a literature-recognised endpoint NAME (PFS, OS,
# DFS...) means, so that recognising the name resolves form + event +
# reference + measurement together instead of donating the name to one
# dimension as a synonym. `conform_row` tries it as step 0, before the
# ordinary per-dimension cascade.
NAMED_ENDPOINTS_FILENAME = "named_endpoints.yaml"

ALL_FILENAMES: tuple[str, ...] = (
    tuple(d.filename for d in DIMENSIONS)
    + (MAPPING_FILENAME, MATCHING_FILENAME, USDM_TEMPLATES_FILENAME, NAMED_ENDPOINTS_FILENAME)
)

# Closed value sets for matching.yaml. `named_endpoint` is step 0 of
# conform_row (docs/EVENT_SEMANTICS_SPEC.md): identification is exact (a
# whole-token/acronym match like any other), but the fields it fills are
# curated-vocabulary expansion rather than text read directly off the row, so
# it is distinguishable from -- and floored slightly below -- `exact`.
MATCH_METHODS = frozenset({"exact", "syntactic_rule", "semantic", "named_endpoint"})
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

# docs/USDM_PROJECTION_INTEGRITY_SPEC.md change 3: what a timepoint_pattern
# category actually names, independent of what shape of text matched it.
TIMEPOINT_ROLES = frozenset({"assessment_time", "observation_window", "event_horizon", "unresolved"})

# docs/USDM_PROJECTION_INTEGRITY_SPEC.md change 2: the closed set of
# `derived` flag values the projection may emit, one per synthesized or
# defaulted endpoint/objective attribute. Grows only by spec change --
# an unlisted value means an announcement the vocabulary has not signed off
# on, which is exactly the unannounced-default failure mode this spec exists
# to make structurally unrepeatable.
DERIVED_ATTRIBUTES = frozenset({"purpose", "reference", "objective"})

# The closed set of tags a USDM syntax template may use. Each one has to be
# resolvable to both a rendered value and a USDM instance to reference -- see
# docs/USDM_ENDPOINTS_API_SPEC.md, "Tag catalogue" -- so this set cannot grow
# without a home for the new tag's value in the projected document.
#
# The per-outcome analysis population is deliberately NOT a tag. It has no good
# position in an endpoint sentence ("... at Week 16 in Safety population"), and
# its real USDM home is Estimand.analysisPopulationId, which needs the estimand
# work. It is still projected -- as an AnalysisPopulation on the study design,
# linked from the endpoint's decomposition -- just not rendered into the text.
USDM_TAGS = frozenset({"measurement", "concept", "reference", "timepoint", "threshold", "scale", "event"})

# Objective templates are keyed on endpoint level, plus the fallback used when
# no endpoint at that level resolved a measurement.
USDM_OBJECTIVE_KEYS = frozenset({"primary", "secondary", "exploratory", "_unresolved"})
