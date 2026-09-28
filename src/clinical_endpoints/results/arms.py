"""Link each results group to the protocol arm it reports, and give it a role.

The results section keys its arms by its own ids (`OG000`, `BG000`), not by
the protocol's `armGroups[]`, so the only link between a dispersion row and
`raw.design_groups` is the arm's title. This module makes that link explicit,
records how it was made, and degrades rather than guesses: a group whose title
matches no arm, or more than one, gets no role.

`conformed.result_group_arm`, one row per (nct_id, results group title):

| `link_method`  | meaning                                                                |
|----------------|------------------------------------------------------------------------|
| `exact_title`  | the group title equals exactly one arm title, case and spacing folded  |
| `title_stem`   | the same after dropping a trailing "arm", "group" or "cohort"          |
| `sole_arm`     | the study registered one arm and reported one group title              |
| NULL           | no link; `link_skip_reason` says why                                   |

`arm_role` folds the registry's `armGroups[].type` into `experimental` or
`control`. `OTHER` and a missing type give no role, unless every intervention
the drug-class arm tier attached to the arm is of kind `control` (a placebo
arm typed `OTHER`), in which case `role_source` is `drug_class`.
`role_conflict` flags an arm whose registry type and drug-class evidence
disagree; the registry type is kept.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

import duckdb

from clinical_endpoints.db import bulk_insert

ROLE_EXPERIMENTAL = "experimental"
ROLE_CONTROL = "control"
ARM_ROLES = (ROLE_EXPERIMENTAL, ROLE_CONTROL)

# armGroups[].type, folded (AACT writes "Placebo Comparator", the API
# PLACEBO_COMPARATOR).
ARM_TYPE_ROLES: dict[str, Optional[str]] = {
    "experimental": ROLE_EXPERIMENTAL,
    "active_comparator": ROLE_CONTROL,
    "placebo_comparator": ROLE_CONTROL,
    "sham_comparator": ROLE_CONTROL,
    "no_intervention": ROLE_CONTROL,
    "other": None,
}
ARM_TYPES = tuple(ARM_TYPE_ROLES)

# Types that assert the arm received no active treatment, so a non-control
# drug class on the arm contradicts them. An active comparator has one by design.
_INACTIVE_TYPES = {"placebo_comparator", "sham_comparator", "no_intervention"}

_STEM_SUFFIX = re.compile(r"\s+(arm|group|cohort)$")

_RESULT_GROUP_ARM_DDL = """
CREATE OR REPLACE TABLE conformed.result_group_arm (
    nct_id VARCHAR,
    group_title VARCHAR,
    arm_title VARCHAR,
    arm_type VARCHAR,
    arm_role VARCHAR,
    role_source VARCHAR,
    role_conflict BOOLEAN,
    link_method VARCHAR,
    link_skip_reason VARCHAR
)
"""

_COLUMNS = (
    "nct_id", "group_title", "arm_title", "arm_type", "arm_role", "role_source",
    "role_conflict", "link_method", "link_skip_reason",
)


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def fold_arm_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    folded = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return folded or None


def title_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return " ".join(value.strip().lower().split()) or None


def _stem(key: str) -> str:
    return _STEM_SUFFIX.sub("", key)


def _unique(candidates: list) -> Optional[tuple]:
    return candidates[0] if len(candidates) == 1 else None


def link_group(
    group_title: Optional[str], arms: list[tuple[str, Optional[str]]], study_group_titles: int
) -> tuple[Optional[tuple[str, Optional[str]]], Optional[str], Optional[str]]:
    """(arm, link_method, skip_reason) for one results group title against
    the study's registered arms, each `(title, group_type)`."""
    key = title_key(group_title)
    if key is None:
        return None, None, "no_group_title"
    if not arms:
        return None, None, "no_protocol_arms"
    # Baseline tables carry a "Total" column across all arms; it is no arm.
    if key == "total":
        return None, None, "total_group"

    exact = [arm for arm in arms if title_key(arm[0]) == key]
    if len(exact) > 1:
        return None, None, "ambiguous_title"
    if exact:
        return exact[0], "exact_title", None

    stem = _stem(key)
    stemmed = [arm for arm in arms if title_key(arm[0]) and _stem(title_key(arm[0])) == stem]
    if len(stemmed) > 1:
        return None, None, "ambiguous_title"
    if stemmed:
        return stemmed[0], "title_stem", None

    if len(arms) == 1 and study_group_titles == 1:
        return arms[0], "sole_arm", None
    return None, None, "no_title_match"


def _role(
    arm_type: Optional[str], class_kinds: Optional[set[str]]
) -> tuple[Optional[str], Optional[str], bool]:
    """(arm_role, role_source, role_conflict)."""
    typed = ARM_TYPE_ROLES.get(arm_type) if arm_type else None
    all_control = bool(class_kinds) and class_kinds == {"control"}
    any_active = bool(class_kinds) and bool(class_kinds - {"control"})

    if typed is not None:
        conflict = (arm_type == "experimental" and all_control) or (
            arm_type in _INACTIVE_TYPES and any_active
        )
        return typed, "group_type", conflict
    if all_control:
        return ROLE_CONTROL, "drug_class", False
    return None, None, False


def write_result_group_arm(con: duckdb.DuckDBPyConnection) -> dict:
    """Replace conformed.result_group_arm from the distinct group titles in
    conformed.endpoint_dispersion. Returns counts by link method."""
    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(_RESULT_GROUP_ARM_DDL)
    if not _table_exists(con, "conformed", "endpoint_dispersion"):
        return {"groups": 0, "links": {}}

    groups = con.execute(
        "SELECT DISTINCT nct_id, group_title FROM conformed.endpoint_dispersion"
    ).fetchall()

    arms_by_nct: dict[str, list[tuple[str, Optional[str]]]] = defaultdict(list)
    if _table_exists(con, "raw", "design_groups"):
        for nct_id, title, group_type in con.execute(
            "SELECT nct_id, title, group_type FROM raw.design_groups WHERE title IS NOT NULL"
        ).fetchall():
            arms_by_nct[nct_id].append((title, fold_arm_type(group_type)))

    kinds_by_arm: dict[tuple[str, str], set[str]] = defaultdict(set)
    if _table_exists(con, "conformed", "arm_drug_class"):
        for nct_id, group_title, kind in con.execute(
            "SELECT nct_id, group_title, kind FROM conformed.arm_drug_class"
        ).fetchall():
            kinds_by_arm[(nct_id, group_title)].add(kind)

    # "Total" and untitled groups are not candidate arms for sole_arm.
    titles_by_nct: dict[str, set[str]] = defaultdict(set)
    for nct_id, group_title in groups:
        key = title_key(group_title)
        if key and key != "total":
            titles_by_nct[nct_id].add(key)

    rows = []
    for nct_id, group_title in groups:
        arm, method, skip = link_group(
            group_title, arms_by_nct.get(nct_id, []), len(titles_by_nct.get(nct_id, ()))
        )
        if arm is None:
            rows.append((nct_id, group_title, None, None, None, None, False, None, skip))
            continue
        arm_title, arm_type = arm
        role, source, conflict = _role(arm_type, kinds_by_arm.get((nct_id, arm_title)))
        rows.append(
            (nct_id, group_title, arm_title, arm_type, role, source, conflict, method, None)
        )

    if rows:
        bulk_insert(con, "conformed.result_group_arm", list(_COLUMNS), rows)

    links: dict[str, int] = defaultdict(int)
    for row in rows:
        links[row[7] or "unlinked"] += 1
    return {"groups": len(rows), "links": dict(sorted(links.items()))}
