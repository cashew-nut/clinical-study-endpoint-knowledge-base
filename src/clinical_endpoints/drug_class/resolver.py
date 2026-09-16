"""Layered intervention -> drug-class resolution, and the curated-vs-NLM diff.

Reads `vocab.drug_class_*` and the raw intervention tables; writes
`conformed.study_drug_class`, `conformed.arm_drug_class` and
`conformed.drug_class_review_queue`. Shaped like `ta/resolver.py`, with two
differences forced by the data:

* Interventions are rows of their own; MeSH descriptors, ancestors and browse
  branches are attached to the study with no link back to an intervention. So
  the intervention layers run per intervention, the MeSH layers per study, and
  the matches are unioned. The arm tier uses only the intervention-level half.
* Kinds do not suppress each other. Within one source item the first layer to
  match a `kind` wins that kind and later layers can still fill the others, so
  pembrolizumab is `pd1_inhibitor` (mechanism) and `monoclonal_antibody`
  (modality). A control match short-circuits its intervention entirely.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import duckdb

from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, normalise
from clinical_endpoints.vocab.schema import normalise_intervention_type

# Lower rank = stronger evidence; decides which (rule_layer, matched_on) is
# recorded when one class is reached more than once for a study.
RULE_LAYERS = (
    "control_rule",
    "term_override",
    "agent_name",
    "name_pattern",
    "ancestor_rule",
    "branch_rule",
    "modality_rule",
    "default",
)
_LAYER_RANK = {layer: i for i, layer in enumerate(RULE_LAYERS)}

_REVIEW_UNCLASSIFIED = "unclassified_agent"


@dataclass(frozen=True)
class DrugClassMapping:
    control_rules: list[tuple[str, re.Pattern]]  # (class_id, pattern), file order
    term_overrides: dict[str, list[str]]  # mesh_term_normalised -> class ids
    agent_names: dict[str, list[str]]  # agent_name_normalised -> class ids
    name_patterns: list[tuple[str, re.Pattern]]  # (class_id, pattern), file order
    ancestor_rules: dict[str, list[str]]  # ancestor_term_normalised -> class ids
    branch_rules: list[tuple[str, re.Pattern]]  # (class_id, pattern), file order
    modality_rules: dict[str, str]  # intervention_type_normalised -> class id
    kinds: dict[str, str]  # class_id -> kind
    precedence: dict[str, int]  # class_id -> precedence (lower wins primary)
    no_rule_matched: str
    no_interventions_on_study: str

    def kind_of(self, class_id: str) -> str:
        return self.kinds.get(class_id, "pharmacologic")


@dataclass(frozen=True)
class ResolvedDrugClass:
    nct_id: str
    drug_class_id: str
    kind: str
    rule_layer: str
    matched_on: Optional[str]
    is_primary: bool


@dataclass(frozen=True)
class ResolvedArmDrugClass:
    nct_id: str
    group_title: str
    drug_class_id: str
    kind: str
    rule_layer: str
    matched_on: Optional[str]
    link_method: str


@dataclass(frozen=True)
class Intervention:
    ordinal: int
    intervention_type: Optional[str]
    name: Optional[str]
    name_normalised: Optional[str]
    other_names_normalised: tuple[str, ...] = ()


def load_drug_class_mapping(
    con: duckdb.DuckDBPyConnection, *, vocab_dir: Path | str | None = None
) -> DrugClassMapping:
    """The `defaults` block is not persisted in vocab.*, so it is read from the YAML."""
    resolved_vocab_dir = Path(vocab_dir) if vocab_dir else default_vocab_dir()
    docs = load_vocab(resolved_vocab_dir)
    defaults = docs["drug_class_mesh_mapping"].get("defaults") or {}

    def _multi(sql: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for key, class_id in con.execute(sql).fetchall():
            out[key].append(class_id)
        return dict(out)

    control_rules = [
        (class_id, re.compile(pattern, re.IGNORECASE))
        for class_id, pattern in con.execute(
            "SELECT drug_class_id, pattern FROM vocab.drug_class_control_rules ORDER BY ordinal"
        ).fetchall()
    ]
    name_patterns = [
        (class_id, re.compile(pattern, re.IGNORECASE))
        for class_id, pattern in con.execute(
            "SELECT drug_class_id, pattern FROM vocab.drug_class_name_patterns ORDER BY ordinal"
        ).fetchall()
    ]
    branch_rules = [
        (class_id, re.compile(pattern, re.IGNORECASE))
        for class_id, pattern in con.execute(
            "SELECT drug_class_id, pattern FROM vocab.drug_class_branch_rules ORDER BY ordinal"
        ).fetchall()
    ]
    modality_rules = {
        type_normalised: class_id
        for type_normalised, class_id in con.execute(
            "SELECT intervention_type_normalised, drug_class_id FROM vocab.drug_class_modality_rules"
        ).fetchall()
    }
    kinds, precedence = {}, {}
    for class_id, kind, prec in con.execute(
        "SELECT id, kind, precedence FROM vocab.drug_classes"
    ).fetchall():
        kinds[class_id] = kind
        precedence[class_id] = int(prec)

    return DrugClassMapping(
        control_rules=control_rules,
        term_overrides=_multi(
            "SELECT mesh_term_normalised, drug_class_id FROM vocab.drug_class_term_overrides"
        ),
        agent_names=_multi(
            "SELECT agent_name_normalised, drug_class_id FROM vocab.drug_class_agent_names"
        ),
        name_patterns=name_patterns,
        ancestor_rules=_multi(
            "SELECT ancestor_term_normalised, drug_class_id FROM vocab.drug_class_ancestor_rules"
        ),
        branch_rules=branch_rules,
        modality_rules=modality_rules,
        kinds=kinds,
        precedence=precedence,
        no_rule_matched=defaults.get("no_rule_matched", _REVIEW_UNCLASSIFIED),
        no_interventions_on_study=defaults.get("no_interventions_on_study", "no_interventions_stated"),
    )


class _ItemMatches:
    """Matches for one source item, first-hit-wins per `kind`."""

    def __init__(self, mapping: DrugClassMapping) -> None:
        self._mapping = mapping
        self._claimed_kinds: set[str] = set()
        self.matches: dict[str, tuple[str, Optional[str]]] = {}

    def claimed(self, kind: str) -> bool:
        return kind in self._claimed_kinds

    def add(self, class_ids: Iterable[str], rule_layer: str, matched_on: Optional[str]) -> None:
        """Record every class whose kind is still open. Kinds are claimed after
        the whole rule is applied, since one rule may carry two classes of the
        same kind (amivantamab is an EGFR inhibitor and a bispecific engager)."""
        wanted = [c for c in class_ids if not self.claimed(self._mapping.kind_of(c))]
        for class_id in wanted:
            self.matches[class_id] = (rule_layer, matched_on)
        self._claimed_kinds.update(self._mapping.kind_of(c) for c in wanted)


def resolve_intervention(
    intervention: Intervention, mapping: DrugClassMapping
) -> dict[str, tuple[str, Optional[str]]]:
    """The intervention-level layers for one intervention: class_id -> (rule_layer, matched_on)."""
    item = _ItemMatches(mapping)
    name = intervention.name_normalised

    # A control short-circuits: a placebo is not a small molecule with a placebo mechanism.
    if name:
        for class_id, pattern in mapping.control_rules:
            if pattern.search(name):
                item.add([class_id], "control_rule", intervention.name)
                return item.matches

    # term_overrides are keyed on MeSH descriptors, but sponsors usually write
    # the same string, so they are tried against the name and aliases too.
    for candidate in (name, *intervention.other_names_normalised):
        if candidate and (class_ids := mapping.term_overrides.get(candidate)):
            item.add(class_ids, "term_override", candidate)

    # Aliases matter: a new molecular entity is often registered under a
    # development code with the generic name only in `otherNames`.
    for candidate in (name, *intervention.other_names_normalised):
        if candidate and (class_ids := mapping.agent_names.get(candidate)):
            item.add(class_ids, "agent_name", candidate)

    for candidate in (name, *intervention.other_names_normalised):
        if not candidate:
            continue
        for class_id, pattern in mapping.name_patterns:
            if item.claimed(mapping.kind_of(class_id)):
                continue
            if pattern.search(candidate):
                item.add([class_id], "name_pattern", candidate)

    type_key = normalise_intervention_type(intervention.intervention_type)
    if type_key and (class_id := mapping.modality_rules.get(type_key)):
        if not item.claimed(mapping.kind_of(class_id)):
            item.add([class_id], "modality_rule", intervention.intervention_type)

    return item.matches


def _resolve_mesh_term(mesh_term: str, mapping: DrugClassMapping) -> dict[str, tuple[str, Optional[str]]]:
    """term_overrides, agent_names and name_patterns against one NLM intervention descriptor."""
    item = _ItemMatches(mapping)
    key = normalise(mesh_term)
    if class_ids := mapping.term_overrides.get(key):
        item.add(class_ids, "term_override", mesh_term)
    if class_ids := mapping.agent_names.get(key):
        item.add(class_ids, "agent_name", mesh_term)
    for class_id, pattern in mapping.name_patterns:
        if item.claimed(mapping.kind_of(class_id)):
            continue
        if pattern.search(key):
            item.add([class_id], "name_pattern", mesh_term)
    return item.matches


def resolve_study_drug_class_matches(
    *,
    interventions: list[Intervention],
    mesh_terms: list[str],
    ancestors: list[str],
    branches: list[str],
    mapping: DrugClassMapping,
    on_intervention: Optional[
        Callable[[Intervention, dict[str, tuple[str, Optional[str]]]], None]
    ] = None,
) -> dict[str, tuple[str, Optional[str]]]:
    """The layered match for one study: class_id -> (rule_layer, matched_on).
    Shared by the bulk resolver and pull-time `--drug-class` filtering.

    `on_intervention(intervention, its_matches)` is called per intervention so
    the bulk resolver can fill the arm tier, tie-break and review queue in one pass.
    """
    matches: dict[str, tuple[str, Optional[str]]] = {}

    def record(item_matches: dict[str, tuple[str, Optional[str]]]) -> None:
        for class_id, (rule_layer, matched_on) in item_matches.items():
            existing = matches.get(class_id)
            if existing is None or _LAYER_RANK[rule_layer] < _LAYER_RANK[existing[0]]:
                matches[class_id] = (rule_layer, matched_on)

    for intervention in interventions:
        item_matches = resolve_intervention(intervention, mapping)
        record(item_matches)
        if on_intervention:
            on_intervention(intervention, item_matches)

    for mesh_term in mesh_terms:
        record(_resolve_mesh_term(mesh_term, mapping))

    for ancestor in ancestors:
        if class_ids := mapping.ancestor_rules.get(normalise(ancestor)):
            item = _ItemMatches(mapping)
            item.add(class_ids, "ancestor_rule", ancestor)
            record(item.matches)

    for branch in branches:
        for class_id, pattern in mapping.branch_rules:
            if pattern.search(branch):
                item = _ItemMatches(mapping)
                item.add([class_id], "branch_rule", branch)
                record(item.matches)
                break

    if not matches:
        default = (
            mapping.no_rule_matched if interventions else mapping.no_interventions_on_study
        )
        matches[default] = ("default", None)

    return matches


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def _where(nct_ids: Optional[list[str]], column: str = "nct_id") -> tuple[str, list]:
    if nct_ids:
        return f"WHERE {column} = ANY(?)", [list(nct_ids)]
    return "", []


def load_interventions(
    con: duckdb.DuckDBPyConnection, *, nct_ids: Optional[list[str]] = None
) -> dict[str, list[Intervention]]:
    """nct_id -> interventions with aliases attached. Empty on a warehouse
    pulled before the intervention tables existed."""
    if not _table_exists(con, "raw", "interventions"):
        return {}
    where, params = _where(nct_ids)

    aliases: dict[tuple[str, int], list[str]] = defaultdict(list)
    if _table_exists(con, "raw", "intervention_other_names"):
        for nct_id, ordinal, other_name_normalised in con.execute(
            f"SELECT nct_id, ordinal, other_name_normalised FROM raw.intervention_other_names {where}",
            params,
        ).fetchall():
            if other_name_normalised:
                aliases[(nct_id, ordinal)].append(other_name_normalised)

    out: dict[str, list[Intervention]] = defaultdict(list)
    for nct_id, ordinal, intervention_type, name, name_normalised in con.execute(
        f"""
        SELECT nct_id, ordinal, intervention_type, name, name_normalised
        FROM raw.interventions {where} ORDER BY nct_id, ordinal
        """,
        params,
    ).fetchall():
        out[nct_id].append(
            Intervention(
                ordinal=ordinal,
                intervention_type=intervention_type,
                name=name,
                name_normalised=name_normalised,
                other_names_normalised=tuple(aliases.get((nct_id, ordinal), ())),
            )
        )
    return dict(out)


def _load_study_terms(
    con: duckdb.DuckDBPyConnection, table: str, column: str, *, nct_ids: Optional[list[str]] = None
) -> dict[str, list[str]]:
    if not _table_exists(con, "raw", table):
        return {}
    where, params = _where(nct_ids)
    out: dict[str, list[str]] = defaultdict(list)
    for nct_id, value in con.execute(
        f"SELECT nct_id, {column} FROM raw.{table} {where}", params
    ).fetchall():
        if value:
            out[nct_id].append(value)
    return dict(out)


def resolve_drug_classes(
    con: duckdb.DuckDBPyConnection,
    *,
    vocab_dir: Path | str | None = None,
    nct_ids: Optional[list[str]] = None,
) -> tuple[list[ResolvedDrugClass], list[ResolvedArmDrugClass], list[tuple]]:
    """(study, class) rows, (arm, class) rows, and review-queue rows. Writes nothing.

    The arm tier uses only the intervention-level layers: study-level MeSH
    signals pushed onto an arm would attribute the drug's class to the placebo arm.
    """
    mapping = load_drug_class_mapping(con, vocab_dir=vocab_dir)

    where, params = _where(nct_ids)
    study_nct_ids = [
        row[0] for row in con.execute(f"SELECT nct_id FROM raw.studies {where}", params).fetchall()
    ]

    interventions_by_nct = load_interventions(con, nct_ids=nct_ids)
    mesh_by_nct = _load_study_terms(con, "browse_interventions", "mesh_term", nct_ids=nct_ids)
    ancestors_by_nct = _load_study_terms(
        con, "browse_intervention_ancestors", "mesh_term", nct_ids=nct_ids
    )
    branches_by_nct = _load_study_terms(
        con, "browse_intervention_branches", "branch_name", nct_ids=nct_ids
    )
    arm_links = _load_arm_links(con, nct_ids=nct_ids)

    resolved: list[ResolvedDrugClass] = []
    arm_resolved: list[ResolvedArmDrugClass] = []
    review: list[tuple] = []

    for nct_id in study_nct_ids:
        interventions = interventions_by_nct.get(nct_id, [])
        unclassified: list[Intervention] = []
        per_intervention: dict[int, dict[str, tuple[str, Optional[str]]]] = {}
        backing: Counter = Counter()  # tie-break: most interventions backing a class

        def note(
            intervention: Intervention, item_matches: dict[str, tuple[str, Optional[str]]]
        ) -> None:
            per_intervention[intervention.ordinal] = item_matches
            backing.update(item_matches.keys())
            if not item_matches:
                unclassified.append(intervention)

        matches = resolve_study_drug_class_matches(
            interventions=interventions,
            mesh_terms=mesh_by_nct.get(nct_id, []),
            ancestors=ancestors_by_nct.get(nct_id, []),
            branches=branches_by_nct.get(nct_id, []),
            mapping=mapping,
            on_intervention=note,
        )

        primary = min(
            matches,
            key=lambda class_id: (
                mapping.precedence.get(class_id, 10**9),
                -backing.get(class_id, 0),
                class_id,
            ),
        )

        for class_id, (rule_layer, matched_on) in matches.items():
            resolved.append(
                ResolvedDrugClass(
                    nct_id=nct_id,
                    drug_class_id=class_id,
                    kind=mapping.kind_of(class_id),
                    rule_layer=rule_layer,
                    matched_on=matched_on,
                    is_primary=(class_id == primary),
                )
            )

        for group_title, ordinal, link_method in arm_links.get(nct_id, []):
            for class_id, (rule_layer, matched_on) in per_intervention.get(ordinal, {}).items():
                arm_resolved.append(
                    ResolvedArmDrugClass(
                        nct_id=nct_id,
                        group_title=group_title,
                        drug_class_id=class_id,
                        kind=mapping.kind_of(class_id),
                        rule_layer=rule_layer,
                        matched_on=matched_on,
                        link_method=link_method,
                    )
                )

        for intervention in unclassified:
            review.append(
                (nct_id, intervention.ordinal, intervention.name, None, _REVIEW_UNCLASSIFIED)
            )

    return resolved, arm_resolved, review


def _load_arm_links(
    con: duckdb.DuckDBPyConnection, *, nct_ids: Optional[list[str]] = None
) -> dict[str, list[tuple[str, int, str]]]:
    if not _table_exists(con, "raw", "arm_interventions"):
        return {}
    where, params = _where(nct_ids)
    out: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for nct_id, group_title, ordinal, link_method in con.execute(
        f"""
        SELECT nct_id, group_title, intervention_ordinal, link_method
        FROM raw.arm_interventions {where}
        """,
        params,
    ).fetchall():
        out[nct_id].append((group_title, ordinal, link_method))
    return dict(out)


def write_study_drug_class(con: duckdb.DuckDBPyConnection, resolved: list[ResolvedDrugClass]) -> int:
    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(
        """
        CREATE OR REPLACE TABLE conformed.study_drug_class (
            nct_id VARCHAR, drug_class_id VARCHAR, kind VARCHAR,
            rule_layer VARCHAR, matched_on VARCHAR, is_primary BOOLEAN
        )
        """
    )
    rows = [
        (r.nct_id, r.drug_class_id, r.kind, r.rule_layer, r.matched_on, r.is_primary)
        for r in resolved
    ]
    if rows:
        con.executemany("INSERT INTO conformed.study_drug_class VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def write_arm_drug_class(con: duckdb.DuckDBPyConnection, resolved: list[ResolvedArmDrugClass]) -> int:
    """Written but not consumed by `endpoints stats`: the results section's
    `outcome_groups.group_key` links to a protocol arm only by title, and how
    often those titles agree has not been measured."""
    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(
        """
        CREATE OR REPLACE TABLE conformed.arm_drug_class (
            nct_id VARCHAR, group_title VARCHAR, drug_class_id VARCHAR, kind VARCHAR,
            rule_layer VARCHAR, matched_on VARCHAR, link_method VARCHAR
        )
        """
    )
    rows = [
        (r.nct_id, r.group_title, r.drug_class_id, r.kind, r.rule_layer, r.matched_on, r.link_method)
        for r in resolved
    ]
    if rows:
        con.executemany("INSERT INTO conformed.arm_drug_class VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def write_drug_class_review_queue(con: duckdb.DuckDBPyConnection, review: list[tuple]) -> int:
    """Its own table: `conform` replaces `conformed.review_queue` wholesale."""
    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    con.execute(
        """
        CREATE OR REPLACE TABLE conformed.drug_class_review_queue (
            nct_id VARCHAR, ordinal INTEGER, name VARCHAR, mesh_term VARCHAR, reason VARCHAR
        )
        """
    )
    if review:
        con.executemany(
            "INSERT INTO conformed.drug_class_review_queue VALUES (?, ?, ?, ?, ?)", review
        )
    return len(review)


def run_drug_class_resolution(
    con: duckdb.DuckDBPyConnection, *, vocab_dir: Path | str | None = None
) -> dict:
    resolved, arm_resolved, review = resolve_drug_classes(con, vocab_dir=vocab_dir)
    write_study_drug_class(con, resolved)
    write_arm_drug_class(con, arm_resolved)
    write_drug_class_review_queue(con, review)

    distribution = Counter(r.drug_class_id for r in resolved if r.is_primary)
    studies = {r.nct_id for r in resolved}
    unclassified_studies = {
        r.nct_id
        for r in resolved
        if r.is_primary and r.drug_class_id in {"unclassified_agent", "no_interventions_stated"}
    }
    return {
        "row_count": len(resolved),
        "study_count": len(studies),
        "arm_row_count": len(arm_resolved),
        "arm_count": len({(r.nct_id, r.group_title) for r in arm_resolved}),
        "review_count": len(review),
        "classified_studies": len(studies) - len(unclassified_studies),
        "distribution": dict(sorted(distribution.items(), key=lambda kv: kv[1], reverse=True)),
    }


def diff_ancestors(
    con: duckdb.DuckDBPyConnection,
    *,
    vocab_dir: Path | str | None = None,
    nct_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Run the curated layers and NLM's ancestry independently over every
    study and return every disagreement, most frequent first. Only classes of
    the same `kind` are compared."""
    mapping = load_drug_class_mapping(con, vocab_dir=vocab_dir)
    interventions_by_nct = load_interventions(con, nct_ids=nct_ids)
    ancestors_by_nct = _load_study_terms(
        con, "browse_intervention_ancestors", "mesh_term", nct_ids=nct_ids
    )

    disagreements: Counter = Counter()
    for nct_id, interventions in interventions_by_nct.items():
        ancestor_classes: dict[str, tuple[str, str]] = {}  # kind -> (class_id, ancestor term)
        for ancestor in ancestors_by_nct.get(nct_id, []):
            for class_id in mapping.ancestor_rules.get(normalise(ancestor), []):
                ancestor_classes.setdefault(mapping.kind_of(class_id), (class_id, ancestor))

        for intervention in interventions:
            curated = resolve_intervention(intervention, mapping)
            for class_id, (rule_layer, matched_on) in curated.items():
                if rule_layer not in ("agent_name", "name_pattern", "term_override"):
                    continue
                kind = mapping.kind_of(class_id)
                if kind not in ancestor_classes:
                    continue
                from_ancestor, ancestor_term = ancestor_classes[kind]
                if from_ancestor != class_id:
                    disagreements[
                        (intervention.name, kind, class_id, rule_layer, from_ancestor, ancestor_term)
                    ] += 1

    return [
        {
            "intervention": name,
            "kind": kind,
            "class_from_curated": curated_class,
            "curated_layer": rule_layer,
            "class_from_ancestor": ancestor_class,
            "ancestor_term": ancestor_term,
            "count": count,
        }
        for (name, kind, curated_class, rule_layer, ancestor_class, ancestor_term), count in sorted(
            disagreements.items(), key=lambda kv: kv[1], reverse=True
        )
    ]


def coverage_summary(con: duckdb.DuckDBPyConnection) -> dict:
    """How much of the corpus the axis covers. `ancestor_studies` and
    `branch_studies` are CT.gov-only signals; AACT publishes neither."""
    def _count(sql: str) -> int:
        return con.execute(sql).fetchone()[0]

    if not _table_exists(con, "raw", "studies"):
        return {"studies": 0, "studies_with_interventions": 0, "resolved": False}
    studies = _count("SELECT count(*) FROM raw.studies")
    has_interventions = (
        _count("SELECT count(DISTINCT nct_id) FROM raw.interventions")
        if _table_exists(con, "raw", "interventions")
        else 0
    )
    if not _table_exists(con, "conformed", "study_drug_class"):
        return {
            "studies": studies,
            "studies_with_interventions": has_interventions,
            "resolved": False,
        }
    return {
        "studies": studies,
        "studies_with_interventions": has_interventions,
        "resolved": True,
        "classified_studies": _count(
            "SELECT count(DISTINCT nct_id) FROM conformed.study_drug_class "
            "WHERE drug_class_id NOT IN ('unclassified_agent', 'no_interventions_stated')"
        ),
        "mechanism_studies": _count(
            "SELECT count(DISTINCT nct_id) FROM conformed.study_drug_class WHERE kind = 'mechanism'"
        ),
        "control_studies": _count(
            "SELECT count(DISTINCT nct_id) FROM conformed.study_drug_class WHERE kind = 'control'"
        ),
        "ancestor_studies": (
            _count("SELECT count(DISTINCT nct_id) FROM raw.browse_intervention_ancestors")
            if _table_exists(con, "raw", "browse_intervention_ancestors")
            else 0
        ),
        "branch_studies": (
            _count("SELECT count(DISTINCT nct_id) FROM raw.browse_intervention_branches")
            if _table_exists(con, "raw", "browse_intervention_branches")
            else 0
        ),
        "arm_rows": (
            _count("SELECT count(*) FROM conformed.arm_drug_class")
            if _table_exists(con, "conformed", "arm_drug_class")
            else 0
        ),
        "review_queue": (
            _count("SELECT count(*) FROM conformed.drug_class_review_queue")
            if _table_exists(con, "conformed", "drug_class_review_queue")
            else 0
        ),
    }
