"""The results-group to protocol-arm link, and `stats --arm-role`.

The link is by title only, so the tests are mostly about when it refuses:
a group that matches no arm, or more than one, gets no role rather than a
guessed one, and every arm-selected distribution prints how many arms it
could not place.
"""

from __future__ import annotations

import json

import duckdb
import pytest
from typer.testing import CliRunner

from clinical_endpoints.cli.main import app
from clinical_endpoints.results.arms import (
    _role,
    fold_arm_type,
    link_group,
    write_result_group_arm,
)
from clinical_endpoints.results.coverage import gate_measurements
from clinical_endpoints.results.stats import (
    NotComputed,
    StatsFilters,
    analysis_distribution,
    sd_distribution,
    stratify_by_arm_role,
)

runner = CliRunner()

ARMS = [("Drug arm", "experimental"), ("Placebo arm", "placebo_comparator")]


# ------------------------------------------------------------------ linking


def test_an_exact_title_links_case_and_spacing_folded():
    arm, method, skip = link_group("  placebo   ARM ", ARMS, 2)
    assert arm == ARMS[1]
    assert method == "exact_title"
    assert skip is None


def test_a_trailing_arm_or_group_is_stemmed_before_matching():
    arm, method, _ = link_group("Placebo Group", ARMS, 2)
    assert arm == ARMS[1]
    assert method == "title_stem"


def test_a_title_matching_no_arm_is_not_guessed():
    assert link_group("Tiotropium 18 mcg", ARMS, 2) == (None, None, "no_title_match")


def test_a_title_matching_two_arms_is_ambiguous_not_the_first():
    arms = [("Drug", "experimental"), ("Drug arm", "active_comparator")]
    # "drug" is an exact match for one and a stem match for the other; the
    # exact match wins, and two exact matches refuse.
    assert link_group("Drug", arms, 2)[1] == "exact_title"
    twins = [("Drug", "experimental"), ("drug", "active_comparator")]
    assert link_group("Drug", twins, 2) == (None, None, "ambiguous_title")


def test_a_single_arm_study_links_its_single_group_whatever_the_title():
    arms = [("Budesonide/Formoterol", "experimental")]
    assert link_group("Drug", arms, 1)[1] == "sole_arm"
    # ...but not when the results section reported more groups than the protocol had arms.
    assert link_group("Drug", arms, 2) == (None, None, "no_title_match")


def test_the_baseline_total_column_is_no_arm():
    assert link_group("Total", [("Total", "other")], 1) == (None, None, "total_group")


def test_a_study_with_no_registered_arms_says_so():
    assert link_group("Drug", [], 1) == (None, None, "no_protocol_arms")


def test_both_backends_arm_type_spellings_fold_together():
    assert fold_arm_type("Placebo Comparator") == fold_arm_type("PLACEBO_COMPARATOR")
    assert fold_arm_type("No Intervention") == "no_intervention"


# ------------------------------------------------------------------ roles


@pytest.mark.parametrize(
    "arm_type, role",
    [
        ("experimental", "experimental"),
        ("active_comparator", "control"),
        ("placebo_comparator", "control"),
        ("sham_comparator", "control"),
        ("no_intervention", "control"),
        ("other", None),
        (None, None),
    ],
)
def test_the_registry_type_folds_to_a_role(arm_type, role):
    assert _role(arm_type, None)[0] == role


def test_an_arm_typed_other_is_control_when_it_only_received_controls():
    assert _role("other", {"control"}) == ("control", "drug_class", False)
    assert _role("other", {"control", "mechanism"}) == (None, None, False)


def test_a_disagreement_keeps_the_registry_type_and_is_flagged():
    assert _role("experimental", {"control"}) == ("experimental", "group_type", True)
    assert _role("placebo_comparator", {"mechanism"}) == ("control", "group_type", True)
    # An active comparator carries an active class by design.
    assert _role("active_comparator", {"mechanism"}) == ("control", "group_type", False)


# ------------------------------------------------------------------ the table


def _arm_rows(con):
    return {
        (nct, title): (method, role, skip)
        for nct, title, method, role, skip in con.execute(
            """
            SELECT nct_id, group_title, link_method, arm_role, link_skip_reason
            FROM conformed.result_group_arm
            """
        ).fetchall()
    }


def test_results_conform_links_every_group_title(results_con):
    rows = _arm_rows(results_con)
    assert rows[("NCT10000001", "Drug")] == ("title_stem", "experimental", None)
    assert rows[("NCT10000001", "Placebo")] == ("title_stem", "control", None)
    assert rows[("NCT10000002", "Drug")] == ("sole_arm", "experimental", None)
    assert rows[("NCT10000001", "Total")] == (None, None, "total_group")


def test_the_link_degrades_without_design_groups(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute("CREATE SCHEMA conformed")
    con.execute(
        "CREATE TABLE conformed.endpoint_dispersion (nct_id VARCHAR, group_title VARCHAR)"
    )
    con.execute("INSERT INTO conformed.endpoint_dispersion VALUES ('NCT1', 'Drug')")
    assert write_result_group_arm(con) == {"groups": 1, "links": {"unlinked": 1}}
    assert con.execute(
        "SELECT link_skip_reason FROM conformed.result_group_arm"
    ).fetchone() == ("no_protocol_arms",)


# ------------------------------------------------------------------ stats


def _group(report, form_id):
    return next(g for g in report["groups"] if g.form_id == form_id)


def test_arm_role_selects_only_that_roles_arms(results_con):
    control = sd_distribution(results_con, StatsFilters(measurement="fev1", arm_role="control"))
    group = _group(control, "change_from_baseline")
    assert group.arms == 1
    assert group.median == pytest.approx(0.29)

    experimental = sd_distribution(
        results_con, StatsFilters(measurement="fev1", arm_role="experimental")
    )
    assert _group(experimental, "change_from_baseline").arms == 3


def test_arm_type_is_the_finer_registry_value(results_con):
    placebo = sd_distribution(
        results_con, StatsFilters(measurement="fev1", arm_type="PLACEBO_COMPARATOR")
    )
    assert _group(placebo, "change_from_baseline").arms == 1
    active = sd_distribution(
        results_con, StatsFilters(measurement="fev1", arm_type="active_comparator")
    )
    assert active["groups"] == []


def test_an_arm_selected_report_carries_the_link_denominator(results_con):
    report = sd_distribution(results_con, StatsFilters(measurement="fev1", arm_role="control"))
    link = report["arm_link"]
    assert link["arms"] == 4
    assert link["with_role"] == 4
    assert link["by_role"] == {"experimental": 3, "control": 1}


def test_the_baseline_total_column_is_counted_not_placed(results_con):
    report = sd_distribution(
        results_con, StatsFilters(measurement="fev1", source="baseline", arm_role="control")
    )
    assert report["groups"] == []
    assert report["arm_link"]["with_role"] == 0
    assert report["arm_link"]["no_role_reasons"] == {"total_group": 2}


def test_stratify_by_arm_role_gives_disjoint_strata(results_con):
    strata = stratify_by_arm_role(results_con, StatsFilters(measurement="fev1"))
    assert [role for role, _ in strata] == ["experimental", "control"]
    arms = [_group(r, "change_from_baseline").arms for _, r in strata]
    unfiltered = _group(sd_distribution(results_con, StatsFilters(measurement="fev1")), "change_from_baseline")
    assert sum(arms) == unfiltered.arms


def test_an_arm_role_filter_picks_the_stratum(results_con):
    strata = stratify_by_arm_role(results_con, StatsFilters(measurement="fev1", arm_role="control"))
    assert [role for role, _ in strata] == ["control"]


def test_unknown_arm_values_are_refused(results_con):
    with pytest.raises(ValueError, match="--arm-role"):
        sd_distribution(results_con, StatsFilters(measurement="fev1", arm_role="placebo"))
    with pytest.raises(ValueError, match="--arm-type"):
        sd_distribution(results_con, StatsFilters(measurement="fev1", arm_type="comparator"))


def test_arm_selection_does_not_apply_to_analyses(results_con):
    """An analysis compares arms; it belongs to no single role."""
    with pytest.raises(ValueError, match="--analyses"):
        analysis_distribution(results_con, StatsFilters(measurement="fev1", arm_role="control"))
    with pytest.raises(ValueError, match="--analyses"):
        stratify_by_arm_role(results_con, StatsFilters(measurement="fev1"), analyses=True)


def test_arm_role_without_the_link_table_says_so(tmp_path, results_warehouse_path):
    import shutil

    path = tmp_path / "copy.duckdb"
    shutil.copy(results_warehouse_path, path)
    con = duckdb.connect(str(path))
    con.execute("DROP TABLE conformed.result_group_arm")
    with pytest.raises(NotComputed, match="result_group_arm"):
        sd_distribution(con, StatsFilters(measurement="fev1", arm_role="control"))
    # ...and an unselected distribution still works.
    assert sd_distribution(con, StatsFilters(measurement="fev1"))["arm_link"] is None
    con.close()


def test_results_coverage_measures_the_link(results_con):
    arms = gate_measurements(results_con)["arms"]
    assert arms["computed"]
    assert arms["link_methods"] == {
        "title_stem": 2, "unlinked: total_group": 2, "sole_arm": 1,
    }
    # Five outcome SDs, all placed; two baseline "Total" SDs, placed nowhere.
    assert arms["sd_rows"] == 7
    assert arms["sd_rows_with_role"] == 5
    assert arms["role_conflicts"] == 0


# ------------------------------------------------------------------ CLI


def test_stats_cli_arm_role_prints_the_link_line(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--arm-role", "control",
         "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    assert "arm_role=control" in result.output
    assert "arm link" in result.output
    assert "4 of 4 usable arm-level SDs" in result.output


def test_stats_cli_stratifies_by_arm_role(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--by", "arm-role", "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    assert "── experimental arms ──" in result.output
    assert "── control arms ──" in result.output


def test_stats_cli_stratified_arm_role_json(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--by", "arm-role", "--json",
         "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stratified_by"] == "arm_role"
    assert [s["arm_role"] for s in payload["strata"]] == ["experimental", "control"]
    assert payload["strata"][1]["arm_link"]["by_role"]["control"] == 1


def test_stats_cli_refuses_arm_role_with_analyses(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--analyses", "--arm-role", "control",
         "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 1
    assert "--analyses" in result.output


def test_results_coverage_cli_reports_the_arm_section(results_warehouse_path):
    result = runner.invoke(app, ["results", "coverage", "--warehouse", results_warehouse_path])
    assert result.exit_code == 0, result.output
    assert "5. Results groups vs protocol arms" in result.output
    assert "5 of 7 arm-level SDs carry an arm role" in result.output
