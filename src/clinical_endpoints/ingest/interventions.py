"""What each study is actually testing, shared by both ingestion backends
(docs/DRUG_CLASS_SPEC.md, phase 1).

`raw.design_outcomes` carries what a study planned to measure and
`raw.design_groups` carries the arms it measured it in. Neither carries the
*intervention* -- the drug, biologic or device the arm received -- so nothing
downstream could group endpoints by what was being tested. These five tables
carry it: the interventions themselves, their sponsor-supplied aliases, the
arm each one was given in, and the two MeSH-derived signals the CT.gov API
exposes about them.

Landing is thin, exactly as `ingest/design.py` and `ingest/results.py` are
thin -- no classification, no vocabulary, no normalisation beyond a lowercase
key. `intervention_type` lands verbatim, as the registry wrote it, for the same
reason every enumerated results field does (see `ingest/results.py`): this
project's build environment cannot reach clinicaltrials.gov or AACT, so the
exact value set is not known here, and a closed enum in the ingest layer would
silently drop the values it had not anticipated. Folding those strings into
classes happens once, downstream and auditably, in `drug_class/resolver.py`.

Costs no extra network on the CT.gov backend. `_fetch_page` sends no `fields`
parameter, so `armsInterventionsModule` and the whole of `derivedSection` are
already inside the payload the pull fetches -- landing them costs warehouse
size, not requests. That is the same finding docs/ENDPOINT_RESULTS_SPEC.md
records for the results section.

CAVEAT, and it is the same one `ingest/ctgov_api.py`'s module docstring
carries: none of the CT.gov field names below have been verified against a
live response, because the sandbox this was built in cannot reach
clinicaltrials.gov (confirmed HTTP 403 on the outbound proxy). Every extractor
here is written to return an empty list rather than raise when a key is absent,
so a wrong guess costs the drug-class axis and leaves the rest of the pull
intact. See docs/DRUG_CLASS_SPEC.md's "Phasing, with a gate" for the four
counts a live pull owes this module.
"""

from __future__ import annotations

from typing import Any, Optional

# --------------------------------------------------------------------- DDL

# One row per intervention the study registered. `ordinal` is the position in
# the source's own list, and is what raw.arm_interventions points at -- neither
# backend gives interventions a stable id of their own (AACT's `interventions.id`
# is a surrogate that is not stable across AACT's rebuilds, the same objection
# docs/ENDPOINT_RESULTS_SPEC.md raises against `outcomes.id`).
INTERVENTIONS_DDL = """
    nct_id VARCHAR, ordinal INTEGER, intervention_type VARCHAR,
    name VARCHAR, name_normalised VARCHAR, description VARCHAR
"""
INTERVENTIONS_COLUMNS: tuple[str, ...] = (
    "nct_id", "ordinal", "intervention_type", "name", "name_normalised", "description",
)

# Sponsor-supplied aliases: brand names, development codes ("MK-3475"), and the
# generic name where the `name` field carries a code. These matter more than
# they look -- a new molecular entity is routinely uncoded by NLM for a year or
# more after registration, so for recent trials the alias list is often the only
# string a curated agent->class entry can match on.
INTERVENTION_OTHER_NAMES_DDL = """
    nct_id VARCHAR, ordinal INTEGER, other_name VARCHAR, other_name_normalised VARCHAR
"""
INTERVENTION_OTHER_NAMES_COLUMNS: tuple[str, ...] = (
    "nct_id", "ordinal", "other_name", "other_name_normalised",
)

# The arm <-> intervention link, and the only table here whose availability
# differs sharply by backend. AACT publishes it as a real join table
# (`ctgov.design_group_interventions`); the CT.gov API expresses it as
# `interventions[].armGroupLabels[]`, a list of arm *labels* that has to be
# matched back to `armGroups[].label` by string. `link_method` records which
# path produced the row so a query can demand the strong one -- see
# docs/DRUG_CLASS_SPEC.md, "Class is an arm property".
ARM_INTERVENTIONS_DDL = """
    nct_id VARCHAR, group_title VARCHAR, intervention_ordinal INTEGER, link_method VARCHAR
"""
ARM_INTERVENTIONS_COLUMNS: tuple[str, ...] = (
    "nct_id", "group_title", "intervention_ordinal", "link_method",
)

#: `link_method` values, strongest first.
LINK_JOIN_TABLE = "join_table"  # AACT's design_group_interventions
LINK_ARM_LABEL = "arm_label"  # CT.gov armGroupLabels[] matched to an armGroups[].label

# MeSH ancestors of the study's coded interventions. Deliberately its own table
# rather than more rows in raw.browse_interventions: both backends write the
# literal 'intervention' into that table's `mesh_type`, so pooling NLM's
# assignment list with its ancestor closure there would silently convert "this
# trial studies pembrolizumab" into "this trial studies antineoplastic agents"
# with no column left to tell them apart.
BROWSE_INTERVENTION_ANCESTORS_DDL = """
    nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, descendant_term VARCHAR
"""
BROWSE_INTERVENTION_ANCESTORS_COLUMNS: tuple[str, ...] = (
    "nct_id", "mesh_term", "mesh_term_normalised", "descendant_term",
)

# The coarse pharmacologic branches CT.gov derives for interventions, mirroring
# raw.browse_condition_branches. NOT the same abbreviation shape: the condition
# branches turned out to be "B" + a MeSH tree code ("BC04" = Neoplasms), and the
# intervention branch abbreviations are not documented anywhere this project can
# reach. They are therefore landed verbatim and matched by `branch_name`, never
# by parsing the abbreviation -- see `drug_class_mesh_mapping.yaml`'s
# `branch_rules`.
BROWSE_INTERVENTION_BRANCHES_DDL = """
    nct_id VARCHAR, branch_abbrev VARCHAR, branch_name VARCHAR
"""
BROWSE_INTERVENTION_BRANCHES_COLUMNS: tuple[str, ...] = (
    "nct_id", "branch_abbrev", "branch_name",
)

#: (table, ddl, columns), in the order a pull lands them. Shared by both
#: backends so `raw._pull_log.source_tables` and the schema reconciliation stay
#: in step with each other.
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
    """The lookup key for a drug name. Deliberately the same shape as
    `vocab/loader.py`'s `normalise` (casefold, collapse whitespace) so a curated
    `agent_names` entry written against one matches the other -- computed here
    rather than trusted from a source column, the precedent `ingest/aact.py` set
    when it declined to trust an unverified `downcase_mesh_term`."""
    if value is None:
        return None
    return " ".join(value.strip().lower().split()) or None


# ------------------------------------------------------- CT.gov API v2 shape


def _get_path(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def extract_ctgov_interventions(study: dict) -> dict[str, list[tuple]]:
    """One study record -> rows for each of the five tables, keyed by table name.

    Returns every table's key even when empty, so a caller can extend a
    per-table accumulator without checking for absence.
    """
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
        # The arm link, and the one place this backend is weaker than AACT.
        # A label that does not appear in armGroups[] is dropped rather than
        # landed against an arm that does not exist: an unlinked intervention
        # is a coverage gap, an intervention linked to the wrong arm is a
        # wrong clinical claim.
        for label in intervention.get("armGroupLabels") or []:
            if label in arm_labels:
                rows["arm_interventions"].append((nct_id, label, ordinal, LINK_ARM_LABEL))

    rows["browse_intervention_ancestors"].extend(_extract_ancestor_rows(study, nct_id))
    rows["browse_intervention_branches"].extend(_extract_branch_rows(study, nct_id))
    return rows


def _extract_ancestor_rows(study: dict, nct_id: str) -> list[tuple]:
    """derivedSection.interventionBrowseModule.ancestors[] -> ancestor rows.

    `descendant_term` is left NULL: the API publishes the ancestor closure for
    the study as a whole, not per descendant, so which coded intervention each
    ancestor came from is not recoverable here. The column exists because AACT
    could in principle carry it, and because a study-level closure is the weaker
    claim -- a resolver that wants "which drug made this an antineoplastic
    trial" needs to know it does not have that.
    """
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
    """derivedSection.interventionBrowseModule.browseBranches[] -> branch rows."""
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
