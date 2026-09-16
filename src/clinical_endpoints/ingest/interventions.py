"""The intervention tables, shared by both backends: the registered
interventions, their sponsor-supplied aliases, the arm each was given in, and
the two MeSH-derived signals the CT.gov API exposes about them.

Landing is thin: no classification, no normalisation beyond a lowercase key.
`intervention_type` lands verbatim because the exact value set is not known;
folding happens in `drug_class/resolver.py`. On the CT.gov backend this costs
no extra requests: `armsInterventionsModule` and `derivedSection` are in the
payload the pull already fetches.

The CT.gov field names below have not been verified against a live response.
Every extractor returns an empty list rather than raising when a key is absent.
"""

from __future__ import annotations

from typing import Any, Optional

# `ordinal` is the position in the source's own list; neither backend gives
# interventions a stable id (AACT's `interventions.id` is a surrogate).
INTERVENTIONS_DDL = """
    nct_id VARCHAR, ordinal INTEGER, intervention_type VARCHAR,
    name VARCHAR, name_normalised VARCHAR, description VARCHAR
"""
INTERVENTIONS_COLUMNS: tuple[str, ...] = (
    "nct_id", "ordinal", "intervention_type", "name", "name_normalised", "description",
)

# Brand names, development codes ("MK-3475"), and the generic name where
# `name` carries a code. For recent trials the alias list is often the only
# string a curated agent entry can match.
INTERVENTION_OTHER_NAMES_DDL = """
    nct_id VARCHAR, ordinal INTEGER, other_name VARCHAR, other_name_normalised VARCHAR
"""
INTERVENTION_OTHER_NAMES_COLUMNS: tuple[str, ...] = (
    "nct_id", "ordinal", "other_name", "other_name_normalised",
)

# AACT publishes a real join table (`ctgov.design_group_interventions`); the
# CT.gov API gives `interventions[].armGroupLabels[]`, matched back to
# `armGroups[].label` by string. `link_method` records which.
ARM_INTERVENTIONS_DDL = """
    nct_id VARCHAR, group_title VARCHAR, intervention_ordinal INTEGER, link_method VARCHAR
"""
ARM_INTERVENTIONS_COLUMNS: tuple[str, ...] = (
    "nct_id", "group_title", "intervention_ordinal", "link_method",
)

LINK_JOIN_TABLE = "join_table"
LINK_ARM_LABEL = "arm_label"

# Its own table rather than more rows in raw.browse_interventions, which
# would conflate "this trial studies pembrolizumab" with "this trial studies
# antineoplastic agents".
BROWSE_INTERVENTION_ANCESTORS_DDL = """
    nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, descendant_term VARCHAR
"""
BROWSE_INTERVENTION_ANCESTORS_COLUMNS: tuple[str, ...] = (
    "nct_id", "mesh_term", "mesh_term_normalised", "descendant_term",
)

# The intervention branch abbreviations are undocumented, so they are landed
# verbatim and matched by `branch_name` (see drug_class_mesh_mapping.yaml's
# `branch_rules`), unlike the condition branches.
BROWSE_INTERVENTION_BRANCHES_DDL = """
    nct_id VARCHAR, branch_abbrev VARCHAR, branch_name VARCHAR
"""
BROWSE_INTERVENTION_BRANCHES_COLUMNS: tuple[str, ...] = (
    "nct_id", "branch_abbrev", "branch_name",
)

INTERVENTION_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("interventions", INTERVENTIONS_DDL, INTERVENTIONS_COLUMNS),
    ("intervention_other_names", INTERVENTION_OTHER_NAMES_DDL, INTERVENTION_OTHER_NAMES_COLUMNS),
    ("arm_interventions", ARM_INTERVENTIONS_DDL, ARM_INTERVENTIONS_COLUMNS),
    (
        "browse_intervention_ancestors",
        BROWSE_INTERVENTION_ANCESTORS_DDL,
        BROWSE_INTERVENTION_ANCESTORS_COLUMNS,
    ),
    (
        "browse_intervention_branches",
        BROWSE_INTERVENTION_BRANCHES_DDL,
        BROWSE_INTERVENTION_BRANCHES_COLUMNS,
    ),
)

INTERVENTION_TABLE_NAMES: tuple[str, ...] = tuple(name for name, _ddl, _cols in INTERVENTION_TABLES)


def normalise_name(value: Optional[str]) -> Optional[str]:
    """Same shape as `vocab/loader.py`'s `normalise`, so a curated
    `agent_names` entry matches a landed name."""
    if value is None:
        return None
    return " ".join(value.strip().lower().split()) or None


def _get_path(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def extract_ctgov_interventions(study: dict) -> dict[str, list[tuple]]:
    """One study record -> rows per table. Every table's key is present even when empty."""
    nct_id = _get_path(study, "protocolSection", "identificationModule", "nctId")
    rows: dict[str, list[tuple]] = {name: [] for name in INTERVENTION_TABLE_NAMES}
    if not nct_id:
        return rows

    interventions = (
        _get_path(study, "protocolSection", "armsInterventionsModule", "interventions") or []
    )
    arm_labels = {
        label
        for arm in (_get_path(study, "protocolSection", "armsInterventionsModule", "armGroups") or [])
        if isinstance(arm, dict) and (label := arm.get("label"))
    }

    for ordinal, intervention in enumerate(interventions):
        if not isinstance(intervention, dict):
            continue
        name = intervention.get("name")
        rows["interventions"].append(
            (
                nct_id,
                ordinal,
                intervention.get("type"),
                name,
                normalise_name(name),
                intervention.get("description"),
            )
        )
        for other_name in intervention.get("otherNames") or []:
            if isinstance(other_name, str) and other_name.strip():
                rows["intervention_other_names"].append(
                    (nct_id, ordinal, other_name, normalise_name(other_name))
                )
        # A label not in armGroups[] is dropped rather than landed against an arm that does not exist.
        for label in intervention.get("armGroupLabels") or []:
            if label in arm_labels:
                rows["arm_interventions"].append((nct_id, label, ordinal, LINK_ARM_LABEL))

    rows["browse_intervention_ancestors"].extend(_extract_ancestor_rows(study, nct_id))
    rows["browse_intervention_branches"].extend(_extract_branch_rows(study, nct_id))
    return rows


def _extract_ancestor_rows(study: dict, nct_id: str) -> list[tuple]:
    """`descendant_term` is left NULL: the API publishes the ancestor closure
    for the study as a whole, not per descendant."""
    ancestors = _get_path(study, "derivedSection", "interventionBrowseModule", "ancestors") or []
    rows = []
    for ancestor in ancestors:
        if not isinstance(ancestor, dict):
            continue
        term = ancestor.get("term")
        if not term:
            continue
        rows.append((nct_id, term, normalise_name(term), None))
    return rows


def _extract_branch_rows(study: dict, nct_id: str) -> list[tuple]:
    branches = (
        _get_path(study, "derivedSection", "interventionBrowseModule", "browseBranches") or []
    )
    rows = []
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        abbrev = branch.get("abbrev")
        name = branch.get("name")
        if not abbrev and not name:
            continue
        rows.append((nct_id, abbrev, name))
    return rows
