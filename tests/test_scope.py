"""The study filters every reporting command shares (`clinical_endpoints.scope`),
and `endpoints help`."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from typer.testing import CliRunner

from clinical_endpoints.cli.main import app
from clinical_endpoints.results.stats import StatsFilters, sd_distribution, stratify_by_drug_class
from clinical_endpoints.scope import ScopeError, StudyScope

runner = CliRunner()


# ------------------------------------------------------------------ parsing


def test_options_split_on_commas_and_normalise_phases():
    scope = StudyScope.from_options(
        ta="respiratory, oncology", org="Pfizer,AbbVie", phase="3,2/3,PHASE1",
        drug_class="statin", since="2020-01-01",
    )
    assert scope.ta == ("respiratory", "oncology")
    assert scope.org == ("Pfizer", "AbbVie")
    assert scope.phase == ("PHASE3", "PHASE2/PHASE3", "PHASE1")
    assert scope.drug_class == ("statin",)
    assert scope.since == dt.date(2020, 1, 1)
    assert not scope.is_empty


def test_an_empty_scope_is_every_study(results_con):
    scope = StudyScope.from_options()
    assert scope.is_empty
    assert scope.predicate("nct_id") == ("TRUE", [])
    assert scope.nct_ids(results_con) is None


@pytest.mark.parametrize("kwargs", [{"phase": "7"}, {"since": "last year"}])
def test_bad_values_raise_scope_error(kwargs):
    with pytest.raises(ScopeError):
        StudyScope.from_options(**kwargs)


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"org": "acme"}, ["NCT10000001"]),
        ({"org": "ACME,beta"}, ["NCT10000001", "NCT10000002"]),
        ({"since": "2022-01-01"}, ["NCT10000002", "NCT10000003"]),
        ({"phase": "3"}, ["NCT10000001", "NCT10000002", "NCT10000003"]),
        ({"phase": "2"}, []),
        ({"drug_class": "muscarinic_antagonist"}, ["NCT10000001"]),
        ({"org": "acme,beta", "since": "2022-01-01"}, ["NCT10000002"]),
    ],
)
def test_filters_or_within_a_flag_and_and_across_flags(results_con, kwargs, expected):
    assert StudyScope.from_options(**kwargs).nct_ids(results_con) == expected


def test_unknown_drug_class_is_named(results_con):
    with pytest.raises(ScopeError, match="not_a_class"):
        StudyScope.from_options(drug_class="not_a_class").validate(results_con)


def test_ta_without_a_resolved_axis_says_so(results_con):
    with pytest.raises(ScopeError, match="study_therapeutic_area"):
        StudyScope.from_options(ta="respiratory").validate(results_con)


# ------------------------------------------------------------------ stats


def test_org_narrows_the_sd_distribution(results_con):
    everyone = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    acme = sd_distribution(results_con, StatsFilters(measurement="fev1", org="Acme"))
    assert everyone["studies_with_sd"] == 2
    assert acme["studies_with_sd"] == 1
    assert acme["studies_conformed"] == 1


def test_endpoint_filters_take_several_values(results_con):
    one = sd_distribution(results_con, StatsFilters(measurement="fev1"))
    both = sd_distribution(
        results_con,
        StatsFilters(measurement=("fev1", "st_georges_respiratory_questionnaire")),
    )
    assert both["studies_conformed"] >= one["studies_conformed"]
    assert sum(g.arms for g in both["groups"]) > sum(g.arms for g in one["groups"])


def test_stratifying_with_a_drug_class_filter_shows_just_those_classes(results_con):
    strata = stratify_by_drug_class(
        results_con,
        StatsFilters(measurement="fev1", drug_class="muscarinic_antagonist"),
    )
    assert [class_id for class_id, _ in strata] == ["muscarinic_antagonist"]


def test_stats_cli_combines_filters_with_by(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--by", "drug-class", "--form",
         "change_from_baseline", "--org", "Acme", "--phase", "3", "--json",
         "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [s["drug_class_id"] for s in payload["strata"]] == ["muscarinic_antagonist"]
    assert payload["strata"][0]["filters"]["org"] == ["Acme"]
    assert payload["strata"][0]["filters"]["phase"] == ["PHASE3"]


def test_stats_cli_prints_the_filters_it_applied(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--org", "Acme,Beta", "--since", "2021-06-01",
         "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    assert "org=Acme,Beta" in result.output
    assert "since=2021-06-01" in result.output


def test_stats_cli_names_an_unknown_drug_class(results_warehouse_path):
    result = runner.invoke(
        app,
        ["stats", "--measurement", "fev1", "--drug-class", "not_a_class",
         "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 1
    assert "not_a_class" in result.output


# ------------------------------------------------------------------ other commands


def test_results_coverage_respects_the_scope(results_warehouse_path):
    result = runner.invoke(
        app, ["results", "coverage", "--org", "Acme", "--warehouse", results_warehouse_path]
    )
    assert result.exit_code == 0, result.output
    assert "Scope: org=Acme (1 studies)" in result.output
    assert "of 1 pulled" in result.output


def test_drug_class_distribution_respects_the_scope(results_warehouse_path):
    result = runner.invoke(
        app,
        ["drug-class", "distribution", "--org", "Beta", "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    assert "muscarinic_antagonist" not in result.output
    assert "of 1 studies" in result.output


def test_a_scope_matching_nothing_says_so_and_exits_cleanly(results_warehouse_path):
    result = runner.invoke(
        app,
        ["drug-class", "coverage", "--org", "Nobody Inc", "--warehouse", results_warehouse_path],
    )
    assert result.exit_code == 0, result.output
    assert "No pulled study matches org=Nobody Inc" in result.output


def test_usdm_coverage_respects_the_scope(results_warehouse_path):
    result = runner.invoke(
        app, ["usdm", "coverage", "--org", "Acme", "--warehouse", results_warehouse_path]
    )
    assert result.exit_code == 0, result.output
    assert "over 1" in " ".join(result.output.split())


def test_every_reporting_command_takes_the_shared_filters():
    commands = [
        ["stats"], ["results", "coverage"], ["drug-class", "distribution"],
        ["drug-class", "coverage"], ["drug-class", "diff-ancestors"], ["ta", "diff-tree"],
        ["usdm", "coverage"], ["review", "list"], ["vocab", "sample"], ["pull"],
    ]
    for command in commands:
        result = runner.invoke(app, [*command, "--help"], terminal_width=200)
        assert result.exit_code == 0, (command, result.output)
        for flag in ("--ta", "--org", "--phase", "--drug-class", "--since"):
            assert flag in result.output, (command, flag)


# ------------------------------------------------------------------ help


def test_help_prints_the_overview():
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0, result.output
    assert "Typical workflow" in result.output
    assert "--org" in result.output


@pytest.mark.parametrize("topic", ["filters", "stats", "pull", "drug-class", "usdm"])
def test_help_topics(topic):
    result = runner.invoke(app, ["help", topic])
    assert result.exit_code == 0, result.output
    assert "uv run endpoints" in result.output or "--ta" in result.output


def test_help_rejects_an_unknown_topic():
    result = runner.invoke(app, ["help", "nonsense"])
    assert result.exit_code == 1
    assert "Topics:" in result.output
