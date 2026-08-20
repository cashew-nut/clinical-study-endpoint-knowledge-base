from __future__ import annotations

import duckdb
import pytest

from clinical_endpoints.db import SCHEMAS
from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, write_vocab_tables
from clinical_endpoints.ta.resolver import (
    diff_tree_vs_pattern,
    filter_raw_tables_by_nct_ids,
    load_ta_mapping,
    match_pattern,
    match_tree,
    resolve_therapeutic_areas,
    run_ta_resolution,
    tree_availability_summary,
    write_study_therapeutic_area,
)


@pytest.fixture(scope="module")
def vocab_dir():
    return default_vocab_dir(__file__)


@pytest.fixture
def con(vocab_dir) -> duckdb.DuckDBPyConnection:
    """A warehouse with the real shipped vocab loaded, so resolver tests exercise
    the actual ta_mesh_mapping.yaml rather than a synthetic stand-in."""
    con = duckdb.connect(":memory:")
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    docs = load_vocab(vocab_dir)
    write_vocab_tables(con, docs, vocab_dir=vocab_dir)

    con.execute(
        """
        CREATE TABLE raw.studies (
            nct_id VARCHAR, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
            start_date DATE, primary_completion_date DATE, brief_title VARCHAR, official_title VARCHAR
        )
        """
    )
    con.execute(
        "CREATE TABLE raw.browse_conditions (nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, mesh_type VARCHAR)"
    )
    con.execute(
        "CREATE TABLE raw.browse_interventions (nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, mesh_type VARCHAR)"
    )
    con.execute("CREATE TABLE raw.conditions (nct_id VARCHAR, name VARCHAR)")
    con.execute("CREATE TABLE raw.mesh_terms (mesh_term VARCHAR, mesh_term_normalised VARCHAR, tree_number VARCHAR)")
    con.execute("CREATE TABLE raw.browse_condition_branches (nct_id VARCHAR, branch_abbrev VARCHAR, branch_name VARCHAR)")
    return con


def _insert_study(con, nct_id: str) -> None:
    con.execute(
        "INSERT INTO raw.studies VALUES (?, 'PHASE3', 'RECRUITING', 'INTERVENTIONAL', '2024-01-01', NULL, ?, ?)",
        [nct_id, f"{nct_id} title", f"{nct_id} official"],
    )


def _insert_condition(con, nct_id: str, mesh_term: str) -> None:
    con.execute(
        "INSERT INTO raw.browse_conditions VALUES (?, ?, ?, 'condition')",
        [nct_id, mesh_term, mesh_term.strip().lower()],
    )


def _insert_intervention(con, nct_id: str, mesh_term: str) -> None:
    con.execute(
        "INSERT INTO raw.browse_interventions VALUES (?, ?, ?, 'intervention')",
        [nct_id, mesh_term, mesh_term.strip().lower()],
    )


# --------------------------------------------------------------- load_ta_mapping


def test_load_ta_mapping_reads_term_overrides(con, vocab_dir):
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    assert mapping.term_overrides["lung neoplasms"] == "oncology"
    assert "totally made up term" not in mapping.term_overrides


def test_load_ta_mapping_reads_defaults_from_yaml_not_vocab_tables(con, vocab_dir):
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    assert mapping.no_pattern_matched == "other"
    assert mapping.no_mesh_terms_on_study == "not_stated"


def test_load_ta_mapping_preserves_file_order_for_term_patterns(con, vocab_dir):
    """oncology's term_patterns rule is listed before infectious_disease's in
    ta_mesh_mapping.yaml -- the resolver depends on that file order for
    first-hit-wins regex matching, so this pins the assumption that DuckDB's
    unordered scan of vocab.ta_mesh_term_patterns preserves insertion order."""
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    ta_order = [ta_id for ta_id, _ in mapping.condition_patterns]
    assert ta_order.index("oncology") < ta_order.index("infectious_disease")


def test_tree_prefixes_sorted_longest_first(con, vocab_dir):
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    lengths = [len(prefix) for prefix, _ in mapping.tree_prefixes]
    assert lengths == sorted(lengths, reverse=True)


# ------------------------------------------------------------- match_tree/pattern


def test_match_tree_longest_prefix_wins(con, vocab_dir):
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    # C05.550 (rheumatology, Joint Diseases) is a deeper prefix than C05 (musculoskeletal_pain)
    assert match_tree("C05.550.123", mapping) == "rheumatology"
    assert match_tree("C05.999", mapping) == "musculoskeletal_pain"
    assert match_tree("Z99", mapping) is None
    assert match_tree(None, mapping) is None


def test_match_pattern_first_hit_wins(con, vocab_dir):
    mapping = load_ta_mapping(con, vocab_dir=vocab_dir)
    assert match_pattern("metastatic carcinoma of the lung", mapping.condition_patterns) == "oncology"
    assert match_pattern("nothing recognisable here", mapping.condition_patterns) is None


# --------------------------------------------------------- resolve_therapeutic_areas


def test_resolve_term_override_layer(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    assert len(resolved) == 1
    r = resolved[0]
    assert (r.ta_id, r.rule_layer, r.matched_on, r.is_primary) == ("oncology", "term_override", "Lung Neoplasms", True)


def test_resolve_keeps_all_matches_and_picks_lowest_precedence_primary(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")  # term_override -> oncology (precedence 10)
    _insert_condition(con, "NCT001", "Asthma")  # term_override -> respiratory (precedence 40)

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    by_ta = {r.ta_id: r for r in resolved}
    assert set(by_ta) == {"oncology", "respiratory"}
    assert by_ta["oncology"].is_primary is True
    assert by_ta["respiratory"].is_primary is False


def test_resolve_vaccine_intervention_rule_beats_condition_precedence(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Meningococcal Infections")  # term_override -> infectious_disease (25)
    _insert_intervention(con, "NCT001", "Meningococcal Vaccine")  # pattern -> vaccines (20)

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    by_ta = {r.ta_id: r for r in resolved}
    assert set(by_ta) == {"infectious_disease", "vaccines"}
    assert by_ta["vaccines"].rule_layer == "intervention_rule"
    assert by_ta["vaccines"].is_primary is True  # vaccines (20) beats infectious_disease (25)


def test_resolve_tree_prefix_layer_from_aact_mesh_terms(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Joint XYZ Disorder")  # not in overrides, no regex hit
    con.execute(
        "INSERT INTO raw.mesh_terms VALUES ('Joint XYZ Disorder', 'joint xyz disorder', 'C05.799.999')"
    )

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    assert len(resolved) == 1
    assert resolved[0].ta_id == "rheumatology"
    assert resolved[0].rule_layer == "tree_prefix"


def test_resolve_tree_prefix_beats_pattern_for_the_same_condition(con, vocab_dir):
    """If a condition's own tree number resolves to a TA, the regex layer must
    not even be tried for that condition (layer order 2 before 3)."""
    _insert_study(con, "NCT001")
    # "arthritis" would hit the rheumatology *pattern*, but give it a tree number
    # that resolves to a different TA to prove tree wins outright.
    _insert_condition(con, "NCT001", "Some Arthritis Variant")
    con.execute(
        "INSERT INTO raw.mesh_terms VALUES ('Some Arthritis Variant', 'some arthritis variant', 'C04.123')"
    )

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    assert len(resolved) == 1
    assert resolved[0].ta_id == "oncology"  # from the C04 tree prefix, not rheumatology from regex
    assert resolved[0].rule_layer == "tree_prefix"


def test_resolve_ctgov_branch_derived_tree_layer(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Some Unrecognisable Descriptor")
    con.execute("INSERT INTO raw.browse_condition_branches VALUES ('NCT001', 'BC04', 'Neoplasms')")

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    ta_ids = {r.ta_id for r in resolved}
    assert "oncology" in ta_ids
    oncology_row = next(r for r in resolved if r.ta_id == "oncology")
    assert oncology_row.rule_layer == "tree_prefix"
    assert oncology_row.matched_on == "Neoplasms"


def test_resolve_default_no_pattern_matched(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Completely Unclassifiable Made Up Term")

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    assert len(resolved) == 1
    assert (resolved[0].ta_id, resolved[0].rule_layer) == ("other", "default")


def test_resolve_default_no_mesh_terms_on_study(con, vocab_dir):
    _insert_study(con, "NCT001")  # no browse_conditions rows at all

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    assert len(resolved) == 1
    assert (resolved[0].ta_id, resolved[0].rule_layer) == ("not_stated", "default")


def test_resolve_scoped_to_nct_ids(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")
    _insert_study(con, "NCT002")
    _insert_condition(con, "NCT002", "Breast Neoplasms")

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir, nct_ids=["NCT001"])
    assert {r.nct_id for r in resolved} == {"NCT001"}


# --------------------------------------------------------- write / run / distribution


def test_write_study_therapeutic_area_is_idempotent(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")

    resolved = resolve_therapeutic_areas(con, vocab_dir=vocab_dir)
    write_study_therapeutic_area(con, resolved)
    write_study_therapeutic_area(con, resolved)

    count = con.execute("SELECT count(*) FROM conformed.study_therapeutic_area").fetchone()[0]
    assert count == 1


def test_run_ta_resolution_summary(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")
    _insert_study(con, "NCT002")
    _insert_condition(con, "NCT002", "Breast Neoplasms")
    _insert_study(con, "NCT003")
    _insert_condition(con, "NCT003", "Asthma")

    summary = run_ta_resolution(con, vocab_dir=vocab_dir)
    assert summary["study_count"] == 3
    assert summary["distribution"]["oncology"] == 2
    assert summary["distribution"]["respiratory"] == 1

    rows = con.execute("SELECT count(*) FROM conformed.study_therapeutic_area").fetchone()[0]
    assert rows == summary["row_count"]


# ---------------------------------------------------------- filter_raw_tables_by_nct_ids


def test_filter_raw_tables_by_nct_ids(con, vocab_dir):
    for nct_id, condition in (("NCT001", "Lung Neoplasms"), ("NCT002", "Breast Neoplasms")):
        _insert_study(con, nct_id)
        _insert_condition(con, nct_id, condition)
    run_ta_resolution(con, vocab_dir=vocab_dir)

    counts = filter_raw_tables_by_nct_ids(con, {"NCT001", "NCT002"}, {"NCT001"})

    assert counts["studies"] == 1
    assert counts["browse_conditions"] == 1
    remaining = con.execute("SELECT nct_id FROM raw.studies").fetchall()
    assert remaining == [("NCT001",)]
    ta_rows = con.execute("SELECT nct_id FROM conformed.study_therapeutic_area").fetchall()
    assert ta_rows == [("NCT001",)]


def test_filter_raw_tables_by_nct_ids_never_touches_studies_outside_this_pull(con, vocab_dir):
    """A `--ta` filter must only drop studies landed by *this* pull -- studies
    from an earlier, unrelated pull that also fail to match must survive."""
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")
    run_ta_resolution(con, vocab_dir=vocab_dir)

    # A later pull lands NCT002 (asthma -- won't match a hypothetical oncology
    # filter) and NCT003 (lung cancer -- matches). NCT001 was never part of
    # this pull at all.
    _insert_study(con, "NCT002")
    _insert_condition(con, "NCT002", "Asthma")
    _insert_study(con, "NCT003")
    _insert_condition(con, "NCT003", "Lung Neoplasms")
    run_ta_resolution(con, vocab_dir=vocab_dir)

    counts = filter_raw_tables_by_nct_ids(con, {"NCT002", "NCT003"}, {"NCT003"})

    assert counts["studies"] == 1  # just NCT003, this pull's kept count
    remaining = {r[0] for r in con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert remaining == {"NCT001", "NCT003"}  # NCT001 untouched, NCT002 dropped
    ta_rows = {r[0] for r in con.execute("SELECT nct_id FROM conformed.study_therapeutic_area").fetchall()}
    assert ta_rows == {"NCT001", "NCT003"}


# --------------------------------------------------------------- diff_tree_vs_pattern


def test_diff_tree_vs_pattern_reports_disagreement(con, vocab_dir):
    _insert_study(con, "NCT001")
    # regex would call this respiratory ("asthma"), but give it a tree number
    # under C04 (oncology) to force a disagreement worth reviewing.
    _insert_condition(con, "NCT001", "Weird Asthma Subtype")
    con.execute(
        "INSERT INTO raw.mesh_terms VALUES ('Weird Asthma Subtype', 'weird asthma subtype', 'C04.999')"
    )

    diffs = diff_tree_vs_pattern(con, vocab_dir=vocab_dir)
    assert len(diffs) == 1
    d = diffs[0]
    assert d["mesh_term"] == "Weird Asthma Subtype"
    assert d["tree_number"] == "C04.999"
    assert d["ta_from_tree"] == "oncology"
    assert d["ta_from_pattern"] == "respiratory"
    assert d["count"] == 1


def test_diff_tree_vs_pattern_excludes_conditions_with_no_tree_number(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Asthma")  # no raw.mesh_terms row for it

    diffs = diff_tree_vs_pattern(con, vocab_dir=vocab_dir)
    assert diffs == []


def test_diff_tree_vs_pattern_excludes_agreement(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Definitely Cancer Thing")
    con.execute(
        "INSERT INTO raw.mesh_terms VALUES "
        "('Definitely Cancer Thing', 'definitely cancer thing', 'C04.123')"
    )
    # regex also resolves this to oncology via "cancer" -> pattern; agreement, not a diff
    diffs = diff_tree_vs_pattern(con, vocab_dir=vocab_dir)
    assert diffs == []


def test_diff_tree_vs_pattern_sorted_most_frequent_first(con, vocab_dir):
    for i in range(3):
        nct_id = f"NCT00{i}"
        _insert_study(con, nct_id)
        _insert_condition(con, nct_id, "Common Disagreement Term")
        con.execute(
            "INSERT INTO raw.mesh_terms VALUES (?, ?, ?)",
            ["Common Disagreement Term", "common disagreement term", "C04.500"],
        )
    _insert_study(con, "NCT999")
    _insert_condition(con, "NCT999", "Rare Asthma Disagreement")
    con.execute(
        "INSERT INTO raw.mesh_terms VALUES "
        "('Rare Asthma Disagreement', 'rare asthma disagreement', 'C04.600')"
    )

    diffs = diff_tree_vs_pattern(con, vocab_dir=vocab_dir)
    assert diffs[0]["count"] >= diffs[-1]["count"]
    assert diffs[0]["mesh_term"] == "Common Disagreement Term"
    assert diffs[0]["count"] == 3


# ------------------------------------------------------------ tree_availability_summary


def test_tree_availability_summary(con, vocab_dir):
    _insert_study(con, "NCT001")
    _insert_condition(con, "NCT001", "Lung Neoplasms")
    con.execute(
        "INSERT INTO raw.mesh_terms VALUES ('Lung Neoplasms', 'lung neoplasms', 'C04.588.894')"
    )
    _insert_study(con, "NCT002")
    _insert_condition(con, "NCT002", "Breast Neoplasms")  # no tree number

    summary = tree_availability_summary(con)
    assert summary["total_condition_rows"] == 2
    assert summary["with_aact_tree_number"] == 1
    assert summary["studies_with_ctgov_branch"] == 0
