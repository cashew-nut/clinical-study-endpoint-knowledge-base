"""Layered MeSH condition/intervention -> therapeutic-area resolution, plus the
tree-vs-pattern diff tool (docs/SAMPLING_AND_TA_RESOLUTION_SPEC.md §§2-3).

Reads the `vocab.ta_mesh_*` tables `vocab validate` writes (term_overrides,
tree_prefixes, term_patterns) and the `raw.browse_conditions` /
`raw.browse_interventions` / `raw.mesh_terms` / `raw.browse_condition_branches`
tables `pull` now lands, and writes `conformed.study_therapeutic_area`.

Layer order, per `vocab/ta_mesh_mapping.yaml`, first hit wins *per condition or
intervention*, but every layer's matches are kept -- a study is many-to-many
with its matched areas (`resolution.keep_all_matches`):

    0. intervention_rules  -- against raw.browse_interventions (vaccines)
    1. term_overrides      -- exact descriptor match
    2. tree_prefixes       -- longest MeSH tree-number prefix wins
    3. term_patterns       -- regex on the descriptor, in file order
    4. defaults            -- no_pattern_matched / no_mesh_terms_on_study
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import duckdb

from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab

# Lower rank = stronger evidence. Used only to decide which (rule_layer,
# matched_on) is recorded when the *same* ta_id is independently reached by
# more than one layer/condition for one study -- every ta_id reached by any
# layer is still kept (`resolution.keep_all_matches`).
RULE_LAYERS = ("intervention_rule", "term_override", "tree_prefix", "term_pattern", "default")
_LAYER_RANK = {layer: i for i, layer in enumerate(RULE_LAYERS)}

# CT.gov API v2's derivedSection.conditionBrowseModule.browseBranches[].abbrev is
# not documented beyond the one example in
# docs/SAMPLING_AND_TA_RESOLUTION_SPEC.md ("BC04" = Neoplasms). Inferred
# convention: "B" + the top-level MeSH tree code, i.e. the same "C04" that
# appears in ta_mesh_mapping.yaml's tree_prefixes. If a live pull ever shows a
# different convention, this is the one place to fix it.
_BRANCH_ABBREV_RE = re.compile(r"^B([A-Z]\d{2})$")

_NCT_KEYED_RAW_TABLES = (
    "studies",
    "design_outcomes",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "browse_condition_branches",
)


@dataclass(frozen=True)
class TaMapping:
    """In-memory snapshot of the layered ta_mesh_mapping, loaded once per call."""

    term_overrides: dict[str, str]  # mesh_term_normalised -> ta_id
    tree_prefixes: list[tuple[str, str]]  # (prefix, ta_id), longest prefix first
    condition_patterns: list[tuple[str, re.Pattern]]  # (ta_id, compiled pattern), file order
    intervention_patterns: list[tuple[str, re.Pattern]]  # (ta_id, compiled pattern), file order
    precedence: dict[str, int]  # ta_id -> precedence (lower wins primary)
    no_pattern_matched: str
    no_mesh_terms_on_study: str


@dataclass(frozen=True)
class ResolvedTa:
    nct_id: str
    ta_id: str
    rule_layer: str
    matched_on: Optional[str]
    is_primary: bool


def load_ta_mapping(
    con: duckdb.DuckDBPyConnection, *, vocab_dir: Path | str | None = None
) -> TaMapping:
    """Read the layered mapping. `term_overrides`/`tree_prefixes`/`term_patterns`
    come from the `vocab.ta_mesh_*` tables `vocab validate` writes (requires
    `vocab validate` to have already been run against this warehouse); the two
    `defaults` values aren't persisted anywhere in vocab.* (the loader never
    writes `ta_mesh_mapping.yaml`'s `defaults` block), so those are read
    straight from the YAML.
    """
    resolved_vocab_dir = Path(vocab_dir) if vocab_dir else default_vocab_dir()
    docs = load_vocab(resolved_vocab_dir)
    defaults = docs["ta_mesh_mapping"].get("defaults") or {}

    term_overrides = {
        mesh_term_normalised: ta_id
        for mesh_term_normalised, ta_id in con.execute(
            "SELECT mesh_term_normalised, ta_id FROM vocab.ta_mesh_term_overrides"
        ).fetchall()
    }

    tree_prefixes = sorted(
        con.execute("SELECT tree_prefix, ta_id FROM vocab.ta_mesh_tree_prefixes").fetchall(),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

    condition_patterns = [
        (ta_id, re.compile(pattern, re.IGNORECASE))
        for ta_id, pattern in con.execute(
            "SELECT ta_id, pattern FROM vocab.ta_mesh_term_patterns WHERE applies_to = 'condition'"
        ).fetchall()
    ]
    intervention_patterns = [
        (ta_id, re.compile(pattern, re.IGNORECASE))
        for ta_id, pattern in con.execute(
            "SELECT ta_id, pattern FROM vocab.ta_mesh_term_patterns WHERE applies_to = 'intervention'"
        ).fetchall()
    ]

    precedence = {
        ta_id: prec
        for ta_id, prec in con.execute("SELECT id, precedence FROM vocab.therapeutic_areas").fetchall()
    }

    return TaMapping(
        term_overrides=term_overrides,
        tree_prefixes=tree_prefixes,
        condition_patterns=condition_patterns,
        intervention_patterns=intervention_patterns,
        precedence=precedence,
        no_pattern_matched=defaults.get("no_pattern_matched", "other"),
        no_mesh_terms_on_study=defaults.get("no_mesh_terms_on_study", "not_stated"),
    )


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def branch_abbrev_to_tree_prefix(abbrev: Optional[str]) -> Optional[str]:
    m = _BRANCH_ABBREV_RE.match(abbrev or "")
    return m.group(1) if m else None


def match_tree(tree_number: Optional[str], mapping: TaMapping) -> Optional[str]:
    """Longest-prefix-wins match of one MeSH tree number against tree_prefixes."""
    if not tree_number:
        return None
    for prefix, ta_id in mapping.tree_prefixes:
        if tree_number == prefix or tree_number.startswith(prefix + "."):
            return ta_id
    return None


def match_pattern(text: Optional[str], patterns: list[tuple[str, re.Pattern]]) -> Optional[str]:
    """First-hit-wins regex match, in file order."""
    if not text:
        return None
    for ta_id, pattern in patterns:
        if pattern.search(text):
            return ta_id
    return None


def _condition_tree_numbers(
    con: duckdb.DuckDBPyConnection, *, nct_ids: Optional[list[str]] = None
) -> dict[tuple[str, str], str]:
    """(nct_id, mesh_term_normalised) -> tree_number, joined from raw.mesh_terms.

    Only populated on the AACT backend, and only once AACT's mesh_terms table
    actually carries tree numbers -- empty (but present, correctly shaped) on
    every other pull, per ingest/aact.py's `_pull_mesh_terms`.
    """
    if not _table_exists(con, "raw", "mesh_terms"):
        return {}
    where = "WHERE bc.nct_id = ANY(?)" if nct_ids else ""
    params = [list(nct_ids)] if nct_ids else []
    rows = con.execute(
        f"""
        SELECT bc.nct_id, bc.mesh_term_normalised, mt.tree_number
        FROM raw.browse_conditions bc
        JOIN raw.mesh_terms mt USING (mesh_term_normalised)
        {where}
        """,
        params,
    ).fetchall()
    return {(nct_id, mesh_term_normalised): tree_number for nct_id, mesh_term_normalised, tree_number in rows}


def _condition_branch_tree_candidates(
    con: duckdb.DuckDBPyConnection, *, nct_ids: Optional[list[str]] = None
) -> dict[str, list[tuple[str, str]]]:
    """nct_id -> [(tree_prefix, branch_name)], derived from CT.gov's coarse
    browseBranches abbreviations. Empty on the AACT backend."""
    if not _table_exists(con, "raw", "browse_condition_branches"):
        return {}
    where = "WHERE nct_id = ANY(?)" if nct_ids else ""
    params = [list(nct_ids)] if nct_ids else []
    rows = con.execute(
        f"SELECT nct_id, branch_abbrev, branch_name FROM raw.browse_condition_branches {where}",
        params,
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nct_id, branch_abbrev, branch_name in rows:
        prefix = branch_abbrev_to_tree_prefix(branch_abbrev)
        if prefix:
            out[nct_id].append((prefix, branch_name or branch_abbrev))
    return out


def resolve_study_ta_matches(
    *,
    conditions: list[tuple[str, str]],
    interventions: list[tuple[str, str]],
    tree_numbers: dict[str, str],
    branch_tree_prefixes: list[tuple[str, str]],
    mapping: TaMapping,
    on_condition_or_branch_match: Optional[Callable[[str], None]] = None,
) -> dict[str, tuple[str, Optional[str]]]:
    """The layered match for *one* study: ta_id -> (rule_layer, matched_on).

    This is the single place the layer order (intervention_rules ->
    term_overrides -> tree_prefixes -> term_patterns -> defaults) is applied
    per condition/intervention -- factored out of `resolve_therapeutic_areas`
    so pull-time `--ta` filtering (ingest/ctgov_api.py, ingest/aact.py) judges
    a study by *exactly* the same rules the bulk resolver later writes to
    conformed.study_therapeutic_area, rather than an approximation that could
    disagree with it.

    `conditions`/`interventions` are (mesh_term, mesh_term_normalised) pairs.
    `tree_numbers` maps a condition's mesh_term_normalised -> tree_number
    (AACT only; empty on the CT.gov API backend, which has no per-condition
    tree number). `branch_tree_prefixes` are (tree_prefix, branch_name) pairs
    already derived from CT.gov's coarse browseBranches (empty on AACT).
    `on_condition_or_branch_match`, if given, is called with each ta_id a
    condition or branch match records -- `resolve_therapeutic_areas` uses
    this to keep its own per-ta_id condition-match tally for the primary tie
    break; callers that only need the match set (pull-time filtering) can
    leave it unset.
    """
    matches: dict[str, tuple[str, Optional[str]]] = {}

    def record(ta_id: str, rule_layer: str, matched_on: Optional[str]) -> None:
        existing = matches.get(ta_id)
        if existing is None or _LAYER_RANK[rule_layer] < _LAYER_RANK[existing[0]]:
            matches[ta_id] = (rule_layer, matched_on)

    for mesh_term, mesh_term_normalised in interventions:
        ta_id = match_pattern(mesh_term_normalised, mapping.intervention_patterns)
        if ta_id:
            record(ta_id, "intervention_rule", mesh_term)

    for mesh_term, mesh_term_normalised in conditions:
        ta_id = mapping.term_overrides.get(mesh_term_normalised)
        rule_layer = "term_override"
        if not ta_id:
            tree_number = tree_numbers.get(mesh_term_normalised)
            ta_id = match_tree(tree_number, mapping)
            rule_layer = "tree_prefix"
        if not ta_id:
            ta_id = match_pattern(mesh_term_normalised, mapping.condition_patterns)
            rule_layer = "term_pattern"
        if ta_id:
            record(ta_id, rule_layer, mesh_term)
            if on_condition_or_branch_match:
                on_condition_or_branch_match(ta_id)

    for tree_prefix, branch_name in branch_tree_prefixes:
        ta_id = match_tree(tree_prefix, mapping)
        if ta_id:
            record(ta_id, "tree_prefix", branch_name)
            if on_condition_or_branch_match:
                on_condition_or_branch_match(ta_id)

    if not matches:
        if conditions:
            record(mapping.no_pattern_matched, "default", None)
        else:
            record(mapping.no_mesh_terms_on_study, "default", None)

    return matches


def resolve_therapeutic_areas(
    con: duckdb.DuckDBPyConnection,
    *,
    vocab_dir: Path | str | None = None,
    nct_ids: Optional[list[str]] = None,
) -> list[ResolvedTa]:
    """Resolve every study's conditions/interventions into (study, ta) rows,
    keeping every matched area and marking the lowest-precedence one primary.

    Does not write anything -- see `write_study_therapeutic_area`.
    """
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)

    where = "WHERE nct_id = ANY(?)" if nct_ids else ""
    params = [list(nct_ids)] if nct_ids else []

    study_nct_ids = [row[0] for row in con.execute(f"SELECT nct_id FROM raw.studies {where}", params).fetchall()]

    conditions_by_nct: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nct_id, mesh_term, mesh_term_normalised in con.execute(
        f"SELECT nct_id, mesh_term, mesh_term_normalised FROM raw.browse_conditions {where}", params
    ).fetchall():
        conditions_by_nct[nct_id].append((mesh_term, mesh_term_normalised))

    interventions_by_nct: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nct_id, mesh_term, mesh_term_normalised in con.execute(
        f"SELECT nct_id, mesh_term, mesh_term_normalised FROM raw.browse_interventions {where}", params
    ).fetchall():
        interventions_by_nct[nct_id].append((mesh_term, mesh_term_normalised))

    tree_by_condition = _condition_tree_numbers(con, nct_ids=nct_ids)
    branch_tree_candidates = _condition_branch_tree_candidates(con, nct_ids=nct_ids)

    resolved: list[ResolvedTa] = []
    for nct_id in study_nct_ids:
        conditions = conditions_by_nct.get(nct_id, [])
        condition_match_counts: Counter = Counter()  # ta_id -> matching-condition count, for tie_break

        matches = resolve_study_ta_matches(
            conditions=conditions,
            interventions=interventions_by_nct.get(nct_id, []),
            tree_numbers={
                mesh_term_normalised: tree_by_condition[(nct_id, mesh_term_normalised)]
                for _mesh_term, mesh_term_normalised in conditions
                if (nct_id, mesh_term_normalised) in tree_by_condition
            },
            branch_tree_prefixes=branch_tree_candidates.get(nct_id, []),
            mapping=mapping,
            on_condition_or_branch_match=lambda ta_id: condition_match_counts.update({ta_id: 1}),
        )

        primary_ta = min(
            matches,
            key=lambda ta_id: (
                mapping.precedence.get(ta_id, 10**9),
                -condition_match_counts.get(ta_id, 0),
                ta_id,
            ),
        )

        for ta_id, (rule_layer, matched_on) in matches.items():
            resolved.append(
                ResolvedTa(
                    nct_id=nct_id,
                    ta_id=ta_id,
                    rule_layer=rule_layer,
                    matched_on=matched_on,
                    is_primary=(ta_id == primary_ta),
                )
            )

    return resolved


def write_study_therapeutic_area(con: duckdb.DuckDBPyConnection, resolved: list[ResolvedTa]) -> int:
    """Replace conformed.study_therapeutic_area wholesale -- it's fully
    recomputed from whatever `resolved` covers (by default every study
    currently in raw.studies), matching how `vocab validate` refreshes
    vocab.*. Unlike raw.* (which `pull` upserts, never dropping studies from
    earlier pulls), this derived table has no "existing" state worth
    preserving independently of its raw.* source."""
    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(
        """
        CREATE OR REPLACE TABLE conformed.study_therapeutic_area (
            nct_id VARCHAR, ta_id VARCHAR, rule_layer VARCHAR, matched_on VARCHAR, is_primary BOOLEAN
        )
        """
    )
    rows = [(r.nct_id, r.ta_id, r.rule_layer, r.matched_on, r.is_primary) for r in resolved]
    if rows:
        con.executemany("INSERT INTO conformed.study_therapeutic_area VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def run_ta_resolution(
    con: duckdb.DuckDBPyConnection, *, vocab_dir: Path | str | None = None
) -> dict:
    """Resolve + write conformed.study_therapeutic_area for every pulled study.
    Returns a summary: row count and the primary-TA distribution."""
    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    write_study_therapeutic_area(con, resolved)
    distribution = Counter(r.ta_id for r in resolved if r.is_primary)
    return {
        "row_count": len(resolved),
        "study_count": len({r.nct_id for r in resolved}),
        "distribution": dict(sorted(distribution.items(), key=lambda kv: kv[1], reverse=True)),
    }


def filter_raw_tables_by_nct_ids(
    con: duckdb.DuckDBPyConnection, pulled_nct_ids: set[str], keep_nct_ids: set[str]
) -> dict[str, int]:
    """Delete rows for studies that *this pull* landed (`pulled_nct_ids`) but
    that don't match `keep_nct_ids`, from every nct_id-keyed raw.* table, plus
    conformed.study_therapeutic_area. Used to apply `--ta` as a post-filter
    after landing an unfiltered pull -- scoped to this pull's studies only, so
    studies landed by earlier pulls (with different filters) are never
    touched, even if they don't match `keep_nct_ids` either. Returns the
    resulting row counts, among this pull's kept studies, for whichever
    tables exist.
    """
    pulled = set(pulled_nct_ids)
    kept_this_pull = list(pulled & set(keep_nct_ids))
    drop_this_pull = list(pulled - set(keep_nct_ids))
    counts: dict[str, int] = {}
    for table in _NCT_KEYED_RAW_TABLES:
        if not _table_exists(con, "raw", table):
            continue
        if drop_this_pull:
            con.execute(f"DELETE FROM raw.{table} WHERE nct_id = ANY(?)", [drop_this_pull])
        counts[table] = con.execute(
            f"SELECT count(*) FROM raw.{table} WHERE nct_id = ANY(?)", [kept_this_pull]
        ).fetchone()[0]
    if drop_this_pull and _table_exists(con, "conformed", "study_therapeutic_area"):
        con.execute(
            "DELETE FROM conformed.study_therapeutic_area WHERE nct_id = ANY(?)", [drop_this_pull]
        )
    return counts


def diff_tree_vs_pattern(
    con: duckdb.DuckDBPyConnection,
    *,
    vocab_dir: Path | str | None = None,
    nct_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Run the tree-prefix layer alone and the regex layer alone over
    every study's browse_conditions, and return every disagreement, most
    frequent first.

    Only conditions with an actual tree number available (from raw.mesh_terms
    on AACT, or a derived coarse prefix from raw.browse_condition_branches on
    the CT.gov API backend) are compared -- a condition with no tree number at
    all is a coverage gap, not a disagreement, and is reported separately by
    `tree_availability_summary`.
    """
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    tree_by_condition = _condition_tree_numbers(con, nct_ids=nct_ids)
    branch_tree_candidates = _condition_branch_tree_candidates(con, nct_ids=nct_ids)

    where = "WHERE nct_id = ANY(?)" if nct_ids else ""
    params = [list(nct_ids)] if nct_ids else []
    condition_rows = con.execute(
        f"SELECT nct_id, mesh_term, mesh_term_normalised FROM raw.browse_conditions {where}", params
    ).fetchall()

    disagreements: Counter = Counter()
    for nct_id, mesh_term, mesh_term_normalised in condition_rows:
        tree_number = tree_by_condition.get((nct_id, mesh_term_normalised))
        if tree_number is None:
            continue  # no tree signal for this specific condition -- not comparable
        ta_from_tree = match_tree(tree_number, mapping)
        ta_from_pattern = match_pattern(mesh_term_normalised, mapping.condition_patterns)
        if ta_from_tree != ta_from_pattern:
            disagreements[(mesh_term, tree_number, ta_from_tree, ta_from_pattern)] += 1

    # The CT.gov backend has no per-condition tree number, only a study-level
    # coarse branch letter -- compare it against every condition of that study
    # (each condition inherits the study's branch-derived candidates).
    conditions_by_nct: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if branch_tree_candidates:
        for nct_id, mesh_term, mesh_term_normalised in condition_rows:
            conditions_by_nct[nct_id].append((mesh_term, mesh_term_normalised))
        for nct_id, candidates in branch_tree_candidates.items():
            for tree_prefix, _branch_name in candidates:
                ta_from_tree = match_tree(tree_prefix, mapping)
                for mesh_term, mesh_term_normalised in conditions_by_nct.get(nct_id, []):
                    ta_from_pattern = match_pattern(mesh_term_normalised, mapping.condition_patterns)
                    if ta_from_tree != ta_from_pattern:
                        disagreements[(mesh_term, tree_prefix, ta_from_tree, ta_from_pattern)] += 1

    return [
        {
            "mesh_term": mesh_term,
            "tree_number": tree_number,
            "ta_from_tree": ta_from_tree,
            "ta_from_pattern": ta_from_pattern,
            "count": count,
        }
        for (mesh_term, tree_number, ta_from_tree, ta_from_pattern), count in sorted(
            disagreements.items(), key=lambda kv: kv[1], reverse=True
        )
    ]


def tree_availability_summary(
    con: duckdb.DuckDBPyConnection, *, nct_ids: Optional[list[str]] = None
) -> dict:
    """How many browse_conditions rows have *any* tree-number signal at all,
    split by source. Reports the coverage gap the diff itself can't see (a
    condition with no tree number never appears in `diff_tree_vs_pattern`)."""
    where = "WHERE nct_id = ANY(?)" if nct_ids else ""
    params = [list(nct_ids)] if nct_ids else []
    total = con.execute(f"SELECT count(*) FROM raw.browse_conditions {where}", params).fetchone()[0]

    tree_by_condition = _condition_tree_numbers(con, nct_ids=nct_ids)
    branch_tree_candidates = _condition_branch_tree_candidates(con, nct_ids=nct_ids)

    return {
        "total_condition_rows": total,
        "with_aact_tree_number": len(tree_by_condition),
        "studies_with_ctgov_branch": len(branch_tree_candidates),
    }
