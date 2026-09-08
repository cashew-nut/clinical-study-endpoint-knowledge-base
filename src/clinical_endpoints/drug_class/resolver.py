"""Layered intervention -> drug-class resolution, plus the curated-vs-NLM diff
tool (docs/DRUG_CLASS_SPEC.md).

Reads the `vocab.drug_class_*` tables `vocab validate` writes and the
`raw.interventions` / `raw.intervention_other_names` / `raw.arm_interventions` /
`raw.browse_interventions` / `raw.browse_intervention_ancestors` /
`raw.browse_intervention_branches` tables `pull` now lands, and writes
`conformed.study_drug_class`, `conformed.arm_drug_class` and
`conformed.drug_class_review_queue`.

Deliberately shaped as `ta/resolver.py` is, including the split that lets
pull-time `--drug-class` filtering and bulk resolution share one per-study
function -- a filter that disagreed with the table written afterwards would be
worse than no filter.

TWO THINGS DIFFER FROM THE TA RESOLVER, and both are forced by the data.

**Matching is per source item, and the items are not all the same grain.** An
intervention is a row of its own (name, type); a MeSH descriptor, an ancestor
and a browse branch are all attached to the STUDY, with no link back to which
intervention they describe. So the intervention layers run per intervention and
the MeSH layers run per study, and their matches are unioned. A study-level
match is therefore a weaker claim than an intervention-level one, which is why
`rule_layer` is recorded on every row and why the arm tier below uses only the
intervention-level half.

**Kinds do not suppress each other.** Within one source item the first layer to
match a given `kind` wins that kind, and later layers can still fill the kinds
still open -- so pembrolizumab is `pd1_inhibitor` (mechanism) AND
`monoclonal_antibody` (modality), and neither defeats the other. See
`drug_class_mesh_mapping.yaml`'s `resolution.modality_is_orthogonal`. The one
exception is a control match, which short-circuits its intervention entirely: a
placebo is not a small molecule with a placebo mechanism, it is a placebo.
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

# Lower rank = stronger evidence. Used only to decide which (rule_layer,
# matched_on) is recorded when the *same* class is independently reached more
# than once for one study -- every class reached by any layer is still kept
# (`resolution.keep_all_matches`).
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
    """In-memory snapshot of the layered mapping, loaded once per call."""

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
    """One registered intervention, with the alias list already attached."""

    ordinal: int
    intervention_type: Optional[str]
    name: Optional[str]
    name_normalised: Optional[str]
    other_names_normalised: tuple[str, ...] = ()


def load_drug_class_mapping(
    con: duckdb.DuckDBPyConnection, *, vocab_dir: Path | str | None = None
) -> DrugClassMapping:
    """Read the layered mapping from the `vocab.drug_class_*` tables (requires
    `vocab validate` to have been run against this warehouse). The `defaults`
    block is not persisted anywhere in vocab.*, so it is read straight from the
    YAML -- the same split `ta/resolver.py`'s `load_ta_mapping` makes."""
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
    """Matches accumulated for ONE source item, enforcing first-hit-wins per
    `kind` -- the rule that lets a mechanism and a modality coexist while
    keeping two mechanisms from the same item's later layers out."""

    def __init__(self, mapping: DrugClassMapping) -> None:
        self._mapping = mapping
        self._claimed_kinds: set[str] = set()
        self.matches: dict[str, tuple[str, Optional[str]]] = {}

    def claimed(self, kind: str) -> bool:
        return kind in self._claimed_kinds

    def add(self, class_ids: Iterable[str], rule_layer: str, matched_on: Optional[str]) -> None:
        """Record every class in `class_ids` whose kind is still open.

        A single rule may legitimately carry two classes of the SAME kind
        (amivantamab is an EGFR inhibitor and a bispecific engager), so the
        kinds are claimed once the whole rule has been applied rather than per
        class -- otherwise the second half of such a rule would be dropped by
        the first half."""
        wanted = [c for c in class_ids if not self.claimed(self._mapping.kind_of(c))]
        for class_id in wanted:
            self.matches[class_id] = (rule_layer, matched_on)
        self._claimed_kinds.update(self._mapping.kind_of(c) for c in wanted)


def resolve_intervention(
    intervention: Intervention, mapping: DrugClassMapping
) -> dict[str, tuple[str, Optional[str]]]:
    """The layered match for ONE intervention: class_id -> (rule_layer, matched_on).

    Layers 0, 2, 3 and 6 of `drug_class_mesh_mapping.yaml` -- the ones that read
    the intervention row itself. The MeSH layers are study-level and live in
    `resolve_study_drug_class_matches`.
    """
    item = _ItemMatches(mapping)
    name = intervention.name_normalised

    # Layer 0. A control short-circuits: no later layer may add to it, because
    # a placebo carries the study's MeSH codes like every other arm and a
    # placebo tablet is not "a small molecule" in any sense worth recording.
    if name:
        for class_id, pattern in mapping.control_rules:
            if pattern.search(name):
                item.add([class_id], "control_rule", intervention.name)
                return item.matches

    # Layer 1: the hand-settled overrides. They are keyed on MeSH descriptors,
    # but for interventions a descriptor IS a drug name and the sponsor usually
    # writes the same string -- so a study registering "Aspirin" must get the
    # same settled answer as one NLM coded as "Aspirin", or the override only
    # applies to half the corpus at random.
    for candidate in (name, *intervention.other_names_normalised):
        if candidate and (class_ids := mapping.term_overrides.get(candidate)):
            item.add(class_ids, "term_override", candidate)

    # Layer 2: the curated dictionary, against the sponsor's own name first and
    # then each alias. Aliases matter more than they look -- a new molecular
    # entity is often registered under a development code with the generic name
    # only in `otherNames`.
    for candidate in (name, *intervention.other_names_normalised):
        if candidate and (class_ids := mapping.agent_names.get(candidate)):
            item.add(class_ids, "agent_name", candidate)

    # Layer 3: WHO INN stems and the vaccine-platform words, in file order.
    for candidate in (name, *intervention.other_names_normalised):
        if not candidate:
            continue
        for class_id, pattern in mapping.name_patterns:
            if item.claimed(mapping.kind_of(class_id)):
                continue
            if pattern.search(candidate):
                item.add([class_id], "name_pattern", candidate)

    # Layer 6: modality from the registry's own type, only where the name did
    # not already say what kind of thing this is.
    type_key = normalise_intervention_type(intervention.intervention_type)
    if type_key and (class_id := mapping.modality_rules.get(type_key)):
        if not item.claimed(mapping.kind_of(class_id)):
            item.add([class_id], "modality_rule", intervention.intervention_type)

    return item.matches


def _resolve_mesh_term(mesh_term: str, mapping: DrugClassMapping) -> dict[str, tuple[str, Optional[str]]]:
    """Layers 1-3 against one NLM intervention descriptor. For interventions the
    descriptor *is* a drug name, so the curated dictionary is reused here rather
    than duplicated -- `term_overrides` exists only for the descriptors whose
    obvious class is the wrong one."""
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
    """The layered match for *one* study: class_id -> (rule_layer, matched_on).

    The single place the layer order is applied, factored out so pull-time
    `--drug-class` filtering judges a study by exactly the same rules the bulk
    resolver later writes to `conformed.study_drug_class` -- the property
    `ta/resolver.py`'s `resolve_study_ta_matches` already guarantees for `--ta`.

    `on_intervention`, if given, is called as
    `on_intervention(intervention, its_matches)` for each intervention as it is
    resolved. It exists so `resolve_drug_classes` can fill the arm tier, the
    primary tie-break and the review queue from this single pass rather than
    re-running `resolve_intervention` over the whole corpus a second time.
    Pull-time filtering, which needs only the match set, leaves it unset.
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


# ------------------------------------------------------------------ bulk read


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
    """nct_id -> its interventions, aliases attached. Empty (not an error) on a
    warehouse pulled before the intervention tables existed."""
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
    """Resolve every study's interventions into (study, class) rows, (arm, class)
    rows, and review-queue rows. Writes nothing -- see the `write_*` functions.

    The arm tier uses only the intervention-level layers, because that is all
    that is attributable to an arm: a study's MeSH descriptors, ancestors and
    browse branches describe the study, and pushing them onto an arm would
    attribute the experimental drug's class to the placebo arm.
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
        # The tie break, per drug_classes.yaml's `resolution.tie_break`: the
        # class backed by the most interventions wins a precedence tie.
        backing: Counter = Counter()

        def note(
            intervention: Intervention, item_matches: dict[str, tuple[str, Optional[str]]]
        ) -> None:
            """Everything this study's per-intervention pass has to yield, taken
            in one walk: the arm tier's input, the tie-break tally, and the
            review queue. Called synchronously below, before the next study
            rebinds these accumulators."""
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


# --------------------------------------------------------------------- write


def write_study_drug_class(con: duckdb.DuckDBPyConnection, resolved: list[ResolvedDrugClass]) -> int:
    """Replace `conformed.study_drug_class` wholesale, matching how
    `write_study_therapeutic_area` refreshes its own table: this is derived from
    raw.*, so it has no state worth preserving independently of its source."""
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
    """Replace `conformed.arm_drug_class` wholesale.

    Written, but deliberately not consumed by `endpoints stats` -- see
    docs/DRUG_CLASS_SPEC.md, "Class is an arm property". `stats` groups the
    results section, whose `outcome_groups.group_key` is a *results* group id
    linked to a protocol arm only by title, and nobody has measured how often
    those titles agree. Until they have, an arm-level SD would be a number built
    on an unmeasured join.
    """
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
    """Replace `conformed.drug_class_review_queue`.

    Its own table rather than rows in `conformed.review_queue`, for the reason
    `conformed.results_review_queue` is its own table: `conform` wholesale-
    replaces that one, and rows kept there would be silently deleted by the next
    protocol-side run.
    """
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
    """Resolve + write all three tables for every pulled study. Returns a
    summary: row counts, the primary-class distribution, and the coverage
    number that keeps the distribution honest."""
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


# ---------------------------------------------------------------------- diff


def diff_ancestors(
    con: duckdb.DuckDBPyConnection,
    *,
    vocab_dir: Path | str | None = None,
    nct_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Run the curated layers alone and NLM's ancestry alone over every study,
    and return every disagreement, most frequent first.

    The counterpart of `endpoints ta diff-tree`, and it has the same discipline:
    it reports, it does not reconcile. Editing the YAML until the diff is empty
    destroys the only external check this axis has -- `agent_names` is a human
    assertion and `ancestor_rules` is NLM's, and where they disagree exactly one
    of them is wrong.

    Only classes of the same `kind` are compared. A curated mechanism class and
    an ancestor-derived pharmacologic one are not in conflict; they are two
    different claims, and the resolver keeps both.
    """
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
    """How much of the corpus the axis actually covers, split the way the
    distribution has to be read: a class list without this is a machine for
    making a thin axis look complete.

    `ancestor_studies` and `branch_studies` are reported because both are
    CT.gov-only signals (AACT publishes neither), so a warehouse pulled from
    AACT will show zero there and a lower mechanism share -- that is a backend
    difference, not a vocabulary failure.
    """
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
