"""D7, D8, D9 and the gate command.

The tests that matter here are the ones about what `stats` refuses to pool and
what it insists on printing: an endpoint statistics reference whose numbers are
right and whose denominators are missing is not a smaller version of the right
answer, it is a machine for producing confident numbers off eight arms.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from clinical_endpoints.cli.main import app
from clinical_endpoints.results.coverage import gate_measurements
from clinical_endpoints.results.effects import classify_effect, null_value, parse_ni_margin
from clinical_endpoints.results.stats import (
    NotComputed,
    StatsFilters,
    analysis_distribution,
    sd_distribution,
)

runner = CliRunner()


def _group(report, form_id):
    return next(g for g in report["groups"] if g.form_id == form_id)


# ------------------------------------------------------------------ D7: SD


def test_sd_distribution_groups_by_form_and_unit(results_con):
    report = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    group = _group(report, "change_from_baseline")
    assert group.scale_id == "litres"
    assert group.converted is True
    assert group.arms == 4
    assert group.studies == 2


def test_litres_and_millilitres_are_pooled_only_after_conversion(results_con):
    """The two studies report FEV1 in different units. Pooling their raw
    numbers would give a median of about 155 -- of nothing."""
    report = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    group = _group(report, "change_from_baseline")
    assert 0.2 < group.median < 0.4
    assert group.scale_id == "litres"


def test_a_change_score_sd_is_never_pooled_with_a_raw_sd(results_con):
    """Converting between them needs the baseline/follow-up correlation, which
    registries do not report. They are different quantities and stay in
    different groups -- and `--source baseline` is how you ask for the second."""
    outcomes = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    assert {g.form_id for g in outcomes["groups"]} == {"change_from_baseline"}

    baseline = sd_distribution(
        results_con, StatsFilters(measurement="fev1", source="baseline")
    )
    assert baseline["groups"]
    assert {g.form_id for g in baseline["groups"]} == {"not_stated"}
    # And they really are different numbers: baseline FEV1 varies far more
    # than the change in it does.
    assert baseline["groups"][0].median > outcomes["groups"][0].median


def test_every_group_carries_its_own_denominator(results_con):
    report = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    for group in report["groups"]:
        assert group.studies_conformed >= group.studies
        assert group.coverage is not None


def test_the_method_mix_is_always_broken_out(results_con):
    """A library built mostly out of range-derived estimates is a different
    object from one built out of reported SDs, so the mix is not optional."""
    group = _group(sd_distribution(results_con, StatsFilters(measurement="fev1")), "change_from_baseline")
    assert group.methods["reported"] == 3
    assert group.methods["from_inter_quartile_range"] == 1


def test_approximate_and_derived_estimates_can_be_excluded(results_con):
    strict = sd_distribution(
        results_con,
        StatsFilters(measurement="fev1", include_approximate=False, include_derived=False),
    )
    group = _group(strict, "change_from_baseline")
    assert set(group.methods) == {"reported"}
    assert group.arms == 3
    # ...and the denominator does not shrink with the numerator.
    assert group.studies_conformed == 2


def test_rows_that_yielded_no_sd_are_reported_as_skip_reasons(results_con):
    """Silence is not a missing value to impute; it is a number to print."""
    report = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    assert report["skip_reasons"]["dispersion_type_unrecognised"] == 1


def test_a_selection_with_no_usable_dispersion_still_reports_its_denominator(results_con):
    report = sd_distribution(results_con, StatsFilters(measurement="adverse_event"))
    assert report["groups"] == []
    assert report["studies_conformed"] == 1
    assert report["skip_reasons"]


def test_filters_narrow_without_changing_the_shape(results_con):
    by_form = sd_distribution(
        results_con, StatsFilters(measurement="fev1", form="change_from_baseline")
    )
    assert len(by_form["groups"]) == 1
    by_scale = sd_distribution(results_con, StatsFilters(measurement="fev1", scale="litres"))
    assert len(by_scale["groups"]) == 1
    assert sd_distribution(results_con, StatsFilters(measurement="fev1", phase="PHASE3"))["groups"]
    assert not sd_distribution(
        results_con, StatsFilters(measurement="fev1", phase="PHASE1")
    )["groups"]


def test_a_single_arm_group_gets_a_quantile_rather_than_an_exception(results_con):
    """One arm having reported a usable SD is the case whose number most needs
    its denominator printed beside it -- not a crash."""
    report = sd_distribution(results_con, StatsFilters(measurement="st_georges_respiratory_questionnaire"))
    group = report["groups"][0]
    assert group.arms == 1
    assert group.median == group.q1 == group.q3


def test_source_must_be_one_of_the_two(results_con):
    with pytest.raises(ValueError, match="--source"):
        sd_distribution(results_con, StatsFilters(measurement="fev1", source="analysis"))


def test_stats_needs_results_conform_to_have_run(tmp_path):
    from clinical_endpoints.db import connect

    con = connect(tmp_path / "warehouse.duckdb")
    with pytest.raises(NotComputed, match="results conform"):
        sd_distribution(con, StatsFilters(measurement="fev1"))


# ------------------------------------------------------------ D9: analyses


def test_effect_measures_are_grouped_by_kind_and_unit(results_con):
    report = analysis_distribution(results_con, StatsFilters(measurement="fev1"))
    kinds = {e["effect_kind"]: e for e in report["effects"]}
    assert "mean_difference" in kinds
    assert kinds["mean_difference"]["scale_id"] == "litres"
    assert kinds["mean_difference"]["null_value"] == 0.0


def test_a_difference_effect_is_converted_before_being_pooled(results_con):
    """0.23 L and 120 mL are 0.23 and 0.12 of the same thing. Their unconverted
    median, 60.1, is a number about nothing."""
    report = analysis_distribution(results_con, StatsFilters(measurement="fev1"))
    mean_difference = next(e for e in report["effects"] if e["effect_kind"] == "mean_difference")
    assert mean_difference["median"] == pytest.approx(0.175)
    assert mean_difference["minimum"] == pytest.approx(0.12)


def test_a_ratio_effect_is_dimensionless_and_pools_on_the_effect_alone(results_con):
    report = analysis_distribution(results_con, StatsFilters())
    hazard = next(e for e in report["effects"] if e["effect_kind"] == "hazard_ratio")
    assert hazard["scale_id"] is None
    assert hazard["null_value"] == 1.0
    assert hazard["median"] == pytest.approx(0.62)


def test_censored_p_values_are_counted_separately_from_observed_ones(results_con):
    report = analysis_distribution(results_con, StatsFilters())
    assert report["p_values"] == {
        "stated": 3,
        "exact": 2,
        "censored": 1,
        "below_0_05": 2,
    }


def test_non_inferiority_margins_are_listed_with_the_text_they_came_from(results_con):
    report = analysis_distribution(results_con, StatsFilters())
    (entry,) = report["non_inferiority"]
    assert entry["nct_id"] == "NCT10000002"
    assert entry["margin"] == -100.0
    assert entry["margin_unit"] == "mL"
    assert entry["margin_source"] == "description"
    assert "Non-inferiority margin" in entry["description"]


def test_the_analysis_report_carries_its_denominator(results_con):
    report = analysis_distribution(results_con, StatsFilters(measurement="fev1"))
    assert report["studies_with_analyses"] <= report["studies_conformed"]
    assert report["studies_conformed"] == 2


# ------------------------------------------------ the NI margin parser alone


@pytest.mark.parametrize(
    "text,value,unit",
    [
        ("Non-inferiority margin of -100 mL", -100.0, "mL"),
        ("NI margin: 10%", 10.0, "%"),
        ("The non-inferiority margin was 1.3", 1.3, None),
        ("margin of 0.5", 0.5, None),
    ],
)
def test_parse_ni_margin_reads_a_stated_margin(text, value, unit):
    margin = parse_ni_margin(text)
    assert margin.value == value
    assert margin.unit == unit
    assert margin.source == "description"


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Non-inferiority was declared",
        # Two candidates after the word and no way to choose between them.
        "margin of 1.3 for the primary and margin of 1.5 for the secondary",
    ],
)
def test_parse_ni_margin_refuses_rather_than_guesses(text):
    """A margin is a regulatory commitment. A guessed one is worse than none,
    and this table does not exist publicly in any form, so it must not be
    seeded with numbers that were never margins."""
    margin = parse_ni_margin(text)
    assert margin.value is None
    assert margin.source is None
    if text:
        assert margin.text == text  # the prose is kept for a reviewer


def test_effect_classification_folds_both_backends_spellings():
    assert classify_effect("Hazard Ratio (HR)") == "hazard_ratio"
    assert classify_effect("HAZARD_RATIO") == "hazard_ratio"
    assert classify_effect("Mean Difference (Net)") == "mean_difference"
    assert classify_effect("Odds Ratio (OR)") == "odds_ratio"
    assert classify_effect("Sponsor's own statistic") == "other"
    assert classify_effect(None) == "unknown"
    assert null_value("other") is None


# ------------------------------------------------------------- the gate


def test_gate_measurements_answer_all_four_questions(results_con):
    report = gate_measurements(results_con)

    posting = report["posting"]
    assert posting["studies_pulled"] == 3
    assert posting["studies_flagged_has_results"] == 2
    assert posting["studies_with_results_landed"] == 2
    assert posting["conformed_studies_with_results"] == 2

    titles = report["titles"]
    assert titles["link_methods"]["exact_title"] == 5
    assert titles["link_methods"]["conformed_measurement"] == 1
    assert titles["link_methods"]["unlinked"] == 1
    assert titles["measurement_unmatched"] == 1

    enums = report["enumerations"]
    assert enums["dispersion_type_raw_distinct"] == 6
    assert {e["value"] for e in enums["dispersion_type_raw_unrecognised"]} == {"Bootstrap Spread"}

    units = report["units"]
    assert units["resolved"] == units["rows"]
    assert units["convertible_to_si"] <= units["rows"]


def test_the_gate_lists_the_exact_strings_the_vocabulary_is_missing(results_con):
    """The point of shipping the gate as a command: after a real pull it names
    which spellings to add, rather than reporting a percentage nobody can act
    on."""
    enums = gate_measurements(results_con)["enumerations"]
    unrecognised = {e["value"]: e["rows"] for e in enums["dispersion_type_raw_unrecognised"]}
    assert unrecognised == {"Bootstrap Spread": 1}


# ------------------------------------------------------------------- the CLI


def test_stats_cli_prints_the_coverage_line(results_warehouse_path):
    result = runner.invoke(
        app, ["stats", "--measurement", "fev1", "--warehouse", results_warehouse_path]
    )
    assert result.exit_code == 0, result.output
    assert "coverage" in result.output
    assert "conformed studies reported a usable dispersion" in result.output


def test_stats_cli_emits_json(results_warehouse_path):
    result = runner.invoke(
        app, ["stats", "--measurement", "fev1", "--json", "--warehouse", results_warehouse_path]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["groups"][0]["scale_id"] == "litres"
    assert payload["groups"][0]["coverage"] == 1.0


def test_stats_cli_analyses_mode(results_warehouse_path):
    result = runner.invoke(
        app, ["stats", "--analyses", "--warehouse", results_warehouse_path]
    )
    assert result.exit_code == 0, result.output
    assert "non-inferiority" in result.output
    assert "p-values" in result.output


def test_stats_cli_rejects_an_unknown_source(results_warehouse_path):
    result = runner.invoke(
        app, ["stats", "--source", "analysis", "--warehouse", results_warehouse_path]
    )
    assert result.exit_code == 1
    assert "--source" in result.output


def test_results_coverage_cli_reports_the_four_sections(results_warehouse_path):
    result = runner.invoke(app, ["results", "coverage", "--warehouse", results_warehouse_path])
    assert result.exit_code == 0, result.output
    for heading in (
        "1. Results posting",
        "2. Reported titles vs planned measures",
        "3. param_type and dispersion_type",
        "4. unit_of_measure against scales.yaml",
    ):
        assert heading in result.output
    assert "Bootstrap Spread" in result.output


def test_results_coverage_cli_needs_a_results_section(tmp_path):
    from clinical_endpoints.db import connect

    warehouse = str(tmp_path / "warehouse.duckdb")
    connect(warehouse).close()
    result = runner.invoke(app, ["results", "coverage", "--warehouse", warehouse])
    assert result.exit_code == 1
    assert "raw.outcome_measures" in result.output


def test_results_conform_cli_reports_what_it_wrote(results_warehouse_path, tmp_path):
    import shutil

    copy = str(tmp_path / "copy.duckdb")
    shutil.copy(results_warehouse_path, copy)
    result = runner.invoke(app, ["results", "conform", "--warehouse", copy])
    assert result.exit_code == 0, result.output
    assert "conformed.endpoint_results" in result.output
    assert "conformed.endpoint_dispersion" in result.output
    assert "Links to planned endpoints" in result.output
