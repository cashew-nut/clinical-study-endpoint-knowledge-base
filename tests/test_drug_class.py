"""The drug-class axis (docs/DRUG_CLASS_SPEC.md): extraction, the layered
resolver, the vocabulary contract, and the two tiers.

The properties worth pinning here are the ones a future change could break
silently: that a control arm is never classed by the study's drug, that a
mechanism and a modality coexist while two mechanisms from one item do not,
that the pull-time filter and the written table agree, and that the arm tier
degrades rather than guesses.
"""

from __future__ import annotations

import duckdb
import pytest

from clinical_endpoints.drug_class.resolver import (
    Intervention,
    coverage_summary,
    diff_ancestors,
    load_drug_class_mapping,
    resolve_intervention,
    resolve_study_drug_class_matches,
    run_drug_class_resolution,
)
from clinical_endpoints.ingest.interventions import (
    INTERVENTION_TABLES,
    extract_ctgov_interventions,
    normalise_name,
)
from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, validate_vocab
from clinical_endpoints.vocab.schema import DRUG_CLASS_KINDS

from pathlib import Path


# --------------------------------------------------------------- extraction


def _study(nct_id="NCT001", *, interventions=None, arms=None, ancestors=None, branches=None):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "armsInterventionsModule": {
                "armGroups": arms or [],
                "interventions": interventions or [],
            },
        },
        "derivedSection": {
            "interventionBrowseModule": {
                "ancestors": ancestors or [],
                "browseBranches": branches or [],
            }
        },
    }


def test_extract_lands_interventions_aliases_and_arm_links():
    rows = extract_ctgov_interventions(
        _study(
            interventions=[
                {
                    "type": "DRUG",
                    "name": "Pembrolizumab",
                    "description": "200 mg Q3W",
                    "otherNames": ["MK-3475", "Keytruda"],
                    "armGroupLabels": ["Pembrolizumab arm"],
                }
            ],
            arms=[{"label": "Pembrolizumab arm"}],
            ancestors=[{"term": "Antineoplastic Agents"}],
            branches=[{"abbrev": "ANeo", "name": "Antineoplastic Agents"}],
        )
    )

    assert rows["interventions"] == [
        ("NCT001", 0, "DRUG", "Pembrolizumab", "pembrolizumab", "200 mg Q3W")
    ]
    assert rows["intervention_other_names"] == [
        ("NCT001", 0, "MK-3475", "mk-3475"),
        ("NCT001", 0, "Keytruda", "keytruda"),
    ]
    assert rows["arm_interventions"] == [("NCT001", "Pembrolizumab arm", 0, "arm_label")]
    assert rows["browse_intervention_ancestors"] == [
        ("NCT001", "Antineoplastic Agents", "antineoplastic agents", None)
    ]
    assert rows["browse_intervention_branches"] == [("NCT001", "ANeo", "Antineoplastic Agents")]


def test_arm_link_is_dropped_when_the_label_names_no_arm():
    """An unlinked intervention is a coverage gap; one linked to an arm that
    does not exist is a wrong clinical claim, so the link is dropped."""
    rows = extract_ctgov_interventions(
        _study(
            interventions=[{"type": "DRUG", "name": "X", "armGroupLabels": ["Typo arm"]}],
            arms=[{"label": "Real arm"}],
        )
    )
    assert rows["interventions"]
    assert rows["arm_interventions"] == []


def test_extract_survives_a_study_with_no_arms_interventions_module():
    rows = extract_ctgov_interventions({"protocolSection": {"identificationModule": {"nctId": "N"}}})
    assert all(v == [] for v in rows.values())


@pytest.mark.parametrize(
    "raw,expected",
    [("  Pembrolizumab  ", "pembrolizumab"), ("MK 3475", "mk 3475"), ("", None), (None, None)],
)
def test_normalise_name(raw, expected):
    assert normalise_name(raw) == expected


# --------------------------------------------------------------- vocabulary


@pytest.fixture(scope="module")
def vocab_docs():
    return load_vocab(default_vocab_dir(Path(__file__).parent))


def test_drug_class_vocabulary_validates(vocab_docs):
    result = validate_vocab(vocab_docs)
    assert result.ok, result.errors[:10]


def test_every_drug_class_declares_a_kind_from_the_closed_set(vocab_docs):
    for term in vocab_docs["drug_class"]["terms"]:
        assert term.get("kind") in DRUG_CLASS_KINDS, term["id"]


def test_a_child_class_outranks_its_parent(vocab_docs):
    """Otherwise a study matching both gets the coarser class as its primary --
    `checkpoint_inhibitor` would beat `pd1_inhibitor` even where the curated
    layer named the specific target."""
    terms = {t["id"]: t for t in vocab_docs["drug_class"]["terms"]}
    for term in terms.values():
        if parent := term.get("parent"):
            assert term["precedence"] < terms[parent]["precedence"], term["id"]


def test_a_parent_never_changes_axis(vocab_docs):
    terms = {t["id"]: t for t in vocab_docs["drug_class"]["terms"]}
    for term in terms.values():
        if parent := term.get("parent"):
            assert terms[parent]["kind"] == term["kind"], term["id"]


def test_control_rules_only_ever_yield_control_classes(vocab_docs):
    """The layer exists to stop a placebo arm being classed as the study's drug;
    a control rule yielding a mechanism class would defeat it entirely."""
    kinds = {t["id"]: t["kind"] for t in vocab_docs["drug_class"]["terms"]}
    for rule in vocab_docs["drug_class_mesh_mapping"]["control_rules"]:
        assert kinds[rule["drug_class"]] == "control", rule


def test_branch_rules_never_make_a_mechanism_claim(vocab_docs):
    kinds = {t["id"]: t["kind"] for t in vocab_docs["drug_class"]["terms"]}
    for rule in vocab_docs["drug_class_mesh_mapping"]["branch_rules"]:
        assert kinds[rule["drug_class"]] == "pharmacologic", rule


def test_validation_rejects_a_child_that_does_not_outrank_its_parent(vocab_docs):
    import copy

    docs = copy.deepcopy(vocab_docs)
    terms = {t["id"]: t for t in docs["drug_class"]["terms"]}
    terms["pd1_inhibitor"]["precedence"] = terms["checkpoint_inhibitor"]["precedence"] + 500
    result = validate_vocab(docs)
    assert any("does not beat its parent" in e for e in result.errors), result.errors[:5]


def test_validation_rejects_an_unknown_kind(vocab_docs):
    import copy

    docs = copy.deepcopy(vocab_docs)
    docs["drug_class"]["terms"][0]["kind"] = "structural"
    result = validate_vocab(docs)
    assert any("`kind` must be one of" in e for e in result.errors), result.errors[:5]


# ----------------------------------------------------------------- resolver


@pytest.fixture(scope="module")
def mapping(tmp_path_factory):
    """A warehouse with only the vocabularies loaded -- everything the mapping
    needs and nothing else."""
    from clinical_endpoints.vocab.loader import write_vocab_tables

    vocab_dir = default_vocab_dir(Path(__file__).parent)
    # Not "vocab.duckdb": DuckDB names the catalog after the file, which would
    # collide with the `vocab` schema the loader writes into.
    path = tmp_path_factory.mktemp("drug_class") / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)
    yield load_drug_class_mapping(con, vocab_dir=vocab_dir)
    con.close()


def _intervention(name, intervention_type="DRUG", *, ordinal=0, aliases=()):
    return Intervention(
        ordinal=ordinal,
        intervention_type=intervention_type,
        name=name,
        name_normalised=normalise_name(name),
        other_names_normalised=tuple(normalise_name(a) for a in aliases),
    )


def test_a_curated_agent_resolves_to_its_mechanism_and_its_modality(mapping):
    """The orthogonality rule: kinds do not suppress each other, so an agent
    carries both claims and neither is a defeat for the other."""
    matches = resolve_intervention(_intervention("Pembrolizumab"), mapping)
    assert matches["pd1_inhibitor"][0] == "agent_name"
    assert matches["monoclonal_antibody"][0] == "name_pattern"
    assert "small_molecule" not in matches  # the modality was already decided


def test_modality_falls_back_to_the_registry_type_only_when_the_name_is_silent(mapping):
    matches = resolve_intervention(_intervention("Carboplatin"), mapping)
    assert matches["platinum_chemotherapy"][0] == "agent_name"
    assert matches["small_molecule"] == ("modality_rule", "DRUG")


def test_a_control_short_circuits_its_intervention(mapping):
    """A placebo carries the study's MeSH codes like every other arm. If any
    later layer ran, a placebo tablet would pick up a modality it has no
    business having."""
    matches = resolve_intervention(_intervention("Placebo"), mapping)
    assert list(matches) == ["placebo"]
    assert matches["placebo"][0] == "control_rule"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Standard of Care chemotherapy", "standard_of_care"),
        ("Sham acupuncture", "sham"),
        ("No intervention", "no_intervention"),
    ],
)
def test_the_other_control_kinds_are_recognised(mapping, name, expected):
    assert list(resolve_intervention(_intervention(name), mapping)) == [expected]


def test_an_inn_stem_classes_an_agent_the_dictionary_has_never_seen(mapping):
    """The point of the stem layer: a drug approved after the vocabulary was
    written still lands, because WHO stems are what made its name."""
    matches = resolve_intervention(_intervention("Fictogliflozin"), mapping)
    assert "sglt2_inhibitor" in matches


def test_an_alias_classes_an_agent_registered_under_a_development_code(mapping):
    matches = resolve_intervention(_intervention("XYZ-1234", aliases=["Semaglutide"]), mapping)
    assert matches["glp1_receptor_agonist"][0] == "agent_name"


def test_an_uncoded_biological_gets_nothing_rather_than_a_wrong_modality(mapping):
    """BIOLOGICAL covers antibodies, vaccines, cell therapies and proteins, so
    any single modality it mapped to would be wrong most of the time."""
    assert resolve_intervention(_intervention("ACME-999", "BIOLOGICAL"), mapping) == {}


def test_one_rule_may_carry_two_classes_of_the_same_kind(mapping):
    """Amivantamab is an EGFR inhibitor and a bispecific engager; forcing a
    choice would make the vocabulary assert something false."""
    matches = resolve_intervention(_intervention("Amivantamab"), mapping)
    assert {"egfr_inhibitor", "bispecific_engager"} <= set(matches)


def test_intervention_type_is_matched_case_and_separator_insensitively(mapping):
    """The two backends spell these differently -- DIETARY_SUPPLEMENT vs
    "Dietary Supplement" -- the same way they spell PRIMARY/Primary."""
    for spelling in ("DIETARY_SUPPLEMENT", "Dietary Supplement", "dietary-supplement"):
        matches = resolve_intervention(_intervention("Vitamin K2", spelling), mapping)
        assert "nutritional_agent" in matches, spelling


def test_a_study_keeps_every_matched_class(mapping):
    """Combination therapy is the norm: a trial of pembrolizumab plus
    carboplatin genuinely is both."""
    matches = resolve_study_drug_class_matches(
        interventions=[
            _intervention("Pembrolizumab", ordinal=0),
            _intervention("Carboplatin", ordinal=1),
        ],
        mesh_terms=[],
        ancestors=[],
        branches=[],
        mapping=mapping,
    )
    assert {"pd1_inhibitor", "platinum_chemotherapy"} <= set(matches)


def test_mesh_ancestors_and_branches_contribute_at_study_level(mapping):
    matches = resolve_study_drug_class_matches(
        interventions=[],
        mesh_terms=[],
        ancestors=["Immune Checkpoint Inhibitors"],
        branches=["Antineoplastic Agents"],
        mapping=mapping,
    )
    assert matches["checkpoint_inhibitor"][0] == "ancestor_rule"
    assert matches["antineoplastic_agent"][0] == "branch_rule"


def test_the_all_drugs_branch_is_deliberately_unmapped(mapping):
    """It sits on essentially every drug study, so mapping it would give every
    such study one identical class and make the axis look far better covered
    than it is."""
    matches = resolve_study_drug_class_matches(
        interventions=[], mesh_terms=[], ancestors=[], branches=["All Drugs and Chemicals"],
        mapping=mapping,
    )
    assert list(matches) == ["no_interventions_stated"]


def test_a_study_with_no_interventions_is_distinguished_from_one_that_matched_nothing(mapping):
    empty = resolve_study_drug_class_matches(
        interventions=[], mesh_terms=[], ancestors=[], branches=[], mapping=mapping
    )
    unmatched = resolve_study_drug_class_matches(
        interventions=[_intervention("ACME-999", "BIOLOGICAL")],
        mesh_terms=[], ancestors=[], branches=[], mapping=mapping,
    )
    assert list(empty) == ["no_interventions_stated"]
    assert list(unmatched) == ["unclassified_agent"]


def test_the_per_intervention_pass_is_reported_to_the_caller(mapping):
    """One walk yields the arm tier, the tie-break tally and the review queue;
    `resolve_drug_classes` would otherwise re-resolve the whole corpus twice."""
    seen = []
    resolve_study_drug_class_matches(
        interventions=[
            _intervention("Pembrolizumab", ordinal=0),
            _intervention("ACME-999", "BIOLOGICAL", ordinal=1),
        ],
        mesh_terms=[], ancestors=[], branches=[], mapping=mapping,
        on_intervention=lambda i, m: seen.append((i.name, sorted(m))),
    )
    assert seen[0][0] == "Pembrolizumab" and "pd1_inhibitor" in seen[0][1]
    assert seen[1] == ("ACME-999", [])


# -------------------------------------------------------------- end to end


@pytest.fixture
def resolved_con(tmp_path):
    """A small warehouse taken all the way through resolution."""
    from clinical_endpoints.vocab.loader import write_vocab_tables

    vocab_dir = default_vocab_dir(Path(__file__).parent)
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS conformed")
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)

    con.execute("CREATE TABLE raw.studies (nct_id VARCHAR)")
    con.execute("INSERT INTO raw.studies VALUES ('NCT001'), ('NCT002')")
    for table, ddl, _columns in INTERVENTION_TABLES:
        con.execute(f"CREATE TABLE raw.{table} ({ddl})")
    con.execute(
        "CREATE TABLE raw.browse_interventions (nct_id VARCHAR, mesh_term VARCHAR, "
        "mesh_term_normalised VARCHAR, mesh_type VARCHAR)"
    )
    con.executemany(
        "INSERT INTO raw.interventions VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("NCT001", 0, "DRUG", "Pembrolizumab", "pembrolizumab", None),
            ("NCT001", 1, "DRUG", "Carboplatin", "carboplatin", None),
            ("NCT001", 2, "DRUG", "Placebo", "placebo", None),
            ("NCT002", 0, "BIOLOGICAL", "ACME-999", "acme-999", None),
        ],
    )
    con.executemany(
        "INSERT INTO raw.arm_interventions VALUES (?, ?, ?, ?)",
        [
            ("NCT001", "Pembro + chemo", 0, "arm_label"),
            ("NCT001", "Pembro + chemo", 1, "arm_label"),
            ("NCT001", "Placebo + chemo", 2, "arm_label"),
            ("NCT001", "Placebo + chemo", 1, "arm_label"),
        ],
    )
    con.executemany(
        "INSERT INTO raw.browse_intervention_ancestors VALUES (?, ?, ?, ?)",
        [("NCT001", "Antineoplastic Agents", "antineoplastic agents", None)],
    )
    run_drug_class_resolution(con, vocab_dir=vocab_dir)
    yield con
    con.close()


def test_the_primary_class_is_the_most_specific_mechanism(resolved_con):
    primary = resolved_con.execute(
        "SELECT drug_class_id FROM conformed.study_drug_class WHERE nct_id = 'NCT001' AND is_primary"
    ).fetchall()
    assert primary == [("pd1_inhibitor",)]


def test_the_control_arm_does_not_inherit_the_experimental_drugs_class(resolved_con):
    """The single most damaging thing this axis could do."""
    rows = resolved_con.execute(
        "SELECT drug_class_id FROM conformed.arm_drug_class "
        "WHERE group_title = 'Placebo + chemo' ORDER BY drug_class_id"
    ).fetchall()
    classes = {r[0] for r in rows}
    assert "pd1_inhibitor" not in classes
    assert "placebo" in classes
    # ...but it does carry the chemotherapy it genuinely also received.
    assert "platinum_chemotherapy" in classes


def test_the_arm_tier_records_how_the_link_was_made(resolved_con):
    methods = {
        r[0] for r in resolved_con.execute("SELECT DISTINCT link_method FROM conformed.arm_drug_class").fetchall()
    }
    assert methods == {"arm_label"}


def test_study_level_mesh_matches_never_reach_an_arm(resolved_con):
    """A study's ancestors describe the study; pushing them onto an arm would
    attribute the experimental drug's class to the placebo arm."""
    layers = {
        r[0]
        for r in resolved_con.execute("SELECT DISTINCT rule_layer FROM conformed.arm_drug_class").fetchall()
    }
    assert "ancestor_rule" not in layers
    assert resolved_con.execute(
        "SELECT count(*) FROM conformed.study_drug_class WHERE rule_layer = 'ancestor_rule'"
    ).fetchone()[0] == 1


def test_an_unclassified_intervention_reaches_the_review_queue(resolved_con):
    assert resolved_con.execute(
        "SELECT nct_id, name, reason FROM conformed.drug_class_review_queue"
    ).fetchall() == [("NCT002", "ACME-999", "unclassified_agent")]


def test_coverage_summary_reports_the_denominator(resolved_con):
    summary = coverage_summary(resolved_con)
    assert summary["studies"] == 2
    assert summary["classified_studies"] == 1
    assert summary["review_queue"] == 1


def test_resolution_is_idempotent(resolved_con):
    before = resolved_con.execute("SELECT count(*) FROM conformed.study_drug_class").fetchone()[0]
    run_drug_class_resolution(resolved_con, vocab_dir=default_vocab_dir(Path(__file__).parent))
    after = resolved_con.execute("SELECT count(*) FROM conformed.study_drug_class").fetchone()[0]
    assert before == after


def test_diff_ancestors_reports_a_curated_vs_nlm_disagreement(resolved_con):
    """Seeded deliberately: the curated layer calls carboplatin platinum
    chemotherapy, and an ancestor of "Aromatase Inhibitors" would call the same
    study something else. The tool reports it rather than reconciling it."""
    resolved_con.execute(
        "INSERT INTO raw.browse_intervention_ancestors VALUES "
        "('NCT001', 'Aromatase Inhibitors', 'aromatase inhibitors', NULL)"
    )
    diffs = diff_ancestors(resolved_con, vocab_dir=default_vocab_dir(Path(__file__).parent))
    pairs = {(d["class_from_curated"], d["class_from_ancestor"]) for d in diffs}
    assert ("pd1_inhibitor", "aromatase_inhibitor") in pairs


def test_resolution_survives_a_warehouse_with_no_intervention_tables(tmp_path):
    """A warehouse pulled before these tables existed must degrade, not fail."""
    from clinical_endpoints.vocab.loader import write_vocab_tables

    vocab_dir = default_vocab_dir(Path(__file__).parent)
    con = duckdb.connect(str(tmp_path / "old.duckdb"))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)
    con.execute("CREATE TABLE raw.studies (nct_id VARCHAR)")
    con.execute("INSERT INTO raw.studies VALUES ('NCT001')")

    summary = run_drug_class_resolution(con, vocab_dir=vocab_dir)
    assert summary["distribution"] == {"no_interventions_stated": 1}
    assert summary["review_count"] == 0
    con.close()


# ------------------------------------------------- pull-time filter parity


def _api_study(nct_id, *, interventions):
    """The minimum CT.gov API v2 shape `run_pull` needs, plus interventions."""
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": nct_id},
            "statusModule": {"overallStatus": "RECRUITING", "startDateStruct": {"date": "2024-01-01"}},
            "designModule": {"phases": ["PHASE3"], "studyType": "INTERVENTIONAL"},
            "outcomesModule": {},
            "conditionsModule": {"conditions": []},
            "armsInterventionsModule": {"armGroups": [], "interventions": interventions},
        },
        "derivedSection": {
            "conditionBrowseModule": {"meshes": [], "browseBranches": []},
            "interventionBrowseModule": {"meshes": [], "ancestors": [], "browseBranches": []},
        },
    }


def test_pull_drug_class_filter_agrees_with_the_table_it_writes(tmp_path, monkeypatch):
    """The property that makes a pull-time filter safe: a study kept by the
    filter must be one the bulk resolver also classes that way. A filter that
    disagreed with the table written afterwards would be worse than no filter.
    """
    from clinical_endpoints.ingest import ctgov_api
    from clinical_endpoints.ingest.ctgov_api import run_pull
    from clinical_endpoints.ingest.filters import PullFilters
    from clinical_endpoints.vocab.loader import write_vocab_tables

    vocab_dir = default_vocab_dir(Path(__file__).parent)
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    for schema in ("raw", "conformed"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)

    studies = [
        _api_study("NCT001", interventions=[{"type": "DRUG", "name": "Semaglutide"}]),
        _api_study("NCT002", interventions=[{"type": "DRUG", "name": "Atorvastatin"}]),
        _api_study("NCT003", interventions=[{"type": "DRUG", "name": "Empagliflozin"}]),
    ]

    class _Resp:
        status_code = 200
        text = ""
        url = "fake"

        def json(self):
            return {"studies": studies}

    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: _Resp())

    run_pull(
        con,
        PullFilters(phases=("3",), limit=500, drug_class=("glp1_receptor_agonist",)),
    )
    landed = {r[0] for r in con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert landed == {"NCT001"}

    run_drug_class_resolution(con, vocab_dir=vocab_dir)
    classed = {
        r[0]
        for r in con.execute(
            "SELECT nct_id FROM conformed.study_drug_class "
            "WHERE drug_class_id = 'glp1_receptor_agonist'"
        ).fetchall()
    }
    assert classed == landed
    con.close()


def test_coverage_survives_a_warehouse_with_nothing_pulled(tmp_path):
    con = duckdb.connect(str(tmp_path / "empty.duckdb"))
    summary = coverage_summary(con)
    assert summary == {"studies": 0, "studies_with_interventions": 0, "resolved": False}
    con.close()


def test_every_curated_agent_resolves_to_the_class_the_vocabulary_claims(mapping, vocab_docs):
    """The whole curated dictionary, end to end through the layer order.

    An earlier layer silently stealing an agent -- a control rule matching a
    drug name, say -- would be invisible in any single-agent test and would
    misclass that agent everywhere it appears. 400-odd assertions is the right
    number here because the failure mode is one entry, not the mechanism.
    """
    agents = vocab_docs["drug_class_mesh_mapping"]["agent_names"]
    wrong = []
    for agent, claimed in agents.items():
        expected = set(claimed if isinstance(claimed, list) else [claimed])
        got = set(resolve_intervention(_intervention(agent), mapping))
        if not expected <= got:
            wrong.append((agent, sorted(expected - got), sorted(got)))
    assert not wrong, wrong[:10]


def test_no_curated_agent_is_swallowed_by_a_control_rule(mapping, vocab_docs):
    agents = vocab_docs["drug_class_mesh_mapping"]["agent_names"]
    stolen = [
        agent
        for agent in agents
        if list(resolve_intervention(_intervention(agent), mapping)) in (["placebo"], ["sham"],
                                                                        ["standard_of_care"],
                                                                        ["no_intervention"])
    ]
    assert stolen == []


def test_every_term_override_resolves_through_the_intervention_path(mapping, vocab_docs):
    """`term_overrides` is matched against the sponsor's name as well as the NLM
    descriptor; if that regressed, the override would settle only half the
    corpus and do it at random."""
    overrides = vocab_docs["drug_class_mesh_mapping"]["term_overrides"]
    wrong = []
    for term, claimed in overrides.items():
        expected = set(claimed if isinstance(claimed, list) else [claimed])
        got = set(resolve_intervention(_intervention(term), mapping))
        if not expected <= got:
            wrong.append((term, sorted(expected - got), sorted(got)))
    assert not wrong, wrong
