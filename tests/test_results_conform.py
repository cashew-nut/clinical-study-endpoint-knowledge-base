"""D5 + D6 end to end: results rows conformed through the existing engine,
linked to the planned endpoints, and their dispersions normalised.

Runs against the `results_warehouse_path` fixture, whose two studies are built
to hit every branch at once -- see tests/conftest.py.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from clinical_endpoints.results.pipeline import NoResults, run_results_conform


def _one(con, sql, *params):
    row = con.execute(sql, list(params)).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------ D5: the link


def test_results_rows_conform_through_the_existing_engine(results_con):
    """No second matcher: a results title resolves to the same vocabulary the
    planned half resolves to, by the same cascade."""
    row = results_con.execute(
        """
        SELECT measurement_id, form_id, measurement_match_method, timepoint_pattern
        FROM conformed.endpoint_results
        WHERE source_id = 'OM1'
        """
    ).fetchone()
    assert row[0] == "fev1"
    assert row[1] == "change_from_baseline"
    assert row[2] is not None  # it recorded how, like every other dimension
    assert row[3] is not None


def test_endpoint_results_shares_its_dimension_columns_with_endpoints(results_con):
    """A query written against the planned half has to work unchanged against
    the reported half -- otherwise moving between them silently means
    rewriting, and a rewrite is where a comparison goes wrong."""
    planned = {
        d[0] for d in results_con.execute("SELECT * FROM conformed.endpoints LIMIT 0").description
    }
    reported = {
        d[0]
        for d in results_con.execute(
            "SELECT * FROM conformed.endpoint_results LIMIT 0"
        ).description
    }
    shared = planned - {"endpoint_id", "usdm_text", "conformed_at"}
    assert shared <= reported


def test_an_identical_title_links_by_exact_title(results_con):
    row = results_con.execute(
        """
        SELECT link_method, planned_endpoint_id IS NOT NULL, link_agrees_on_form
        FROM conformed.endpoint_results WHERE source_id = 'OM1'
        """
    ).fetchone()
    assert row == ("exact_title", True, True)


def test_a_reworded_title_links_by_the_conformed_measurement(results_con):
    """"Time from randomisation to death from any cause" and "Overall
    Survival" share no words. They share a `measurement_id`, which is the
    whole reason the vocabulary is a join key."""
    row = results_con.execute(
        """
        SELECT r.link_method, r.measurement_id, e.measure_raw
        FROM conformed.endpoint_results r
        JOIN conformed.endpoints e ON e.endpoint_id = r.planned_endpoint_id
        WHERE r.source_id = 'OM2'
        """
    ).fetchone()
    assert row == ("conformed_measurement", "vital_status", "Overall Survival")


def test_an_outcome_that_was_never_registered_is_kept_and_flagged(results_con):
    """Sponsors report outcomes they never registered. Force-joining one to
    the nearest planned endpoint would be the single most damaging thing this
    pipeline could do, so it is kept unlinked and queued."""
    assert (
        _one(results_con, "SELECT link_method FROM conformed.endpoint_results WHERE source_id = 'OM4'")
        is None
    )
    assert _one(
        results_con,
        "SELECT reason FROM conformed.results_review_queue WHERE source_id = 'OM4'",
    ) == "unlinked_to_planned"
    # ...and it still conformed, so it still counts toward the vocabulary.
    assert _one(
        results_con,
        "SELECT measurement_id FROM conformed.endpoint_results WHERE source_id = 'OM4'",
    ) == "st_georges_respiratory_questionnaire"


def test_a_results_title_that_conforms_nowhere_goes_to_the_results_review_queue(results_con):
    assert not results_con.execute(
        "SELECT 1 FROM conformed.endpoint_results WHERE source_id = 'OM5'"
    ).fetchone()
    assert _one(
        results_con,
        "SELECT reason FROM conformed.results_review_queue WHERE source_id = 'OM5'",
    ) == "measurement_unmatched"


def test_the_results_review_queue_is_its_own_table(results_con):
    """`conform` wholesale-replaces conformed.review_queue. Results rows kept
    there would be silently deleted by the next protocol-side run, so they
    have their own queue with its own lifecycle."""
    planned_reasons = {
        row[0] for row in results_con.execute(
            "SELECT DISTINCT reason FROM conformed.review_queue"
        ).fetchall()
    }
    results_reasons = {
        row[0] for row in results_con.execute(
            "SELECT DISTINCT reason FROM conformed.results_review_queue"
        ).fetchall()
    }
    assert "unlinked_to_planned" in results_reasons
    assert "unlinked_to_planned" not in planned_reasons


def test_baseline_characteristics_conform_but_are_never_queued_as_unlinked(results_con):
    """A baseline characteristic has no planned counterpart by construction.
    Queueing it as "unlinked" would bury the reported outcomes that genuinely
    are."""
    kinds = dict(
        results_con.execute(
            "SELECT result_kind, count(*) FROM conformed.endpoint_results GROUP BY 1"
        ).fetchall()
    )
    assert kinds["baseline"] == 2
    assert not results_con.execute(
        "SELECT 1 FROM conformed.results_review_queue WHERE result_kind = 'baseline'"
    ).fetchone()


def test_a_baseline_characteristic_conforms_once_however_many_arms_reported_it(results_con):
    """The link table is at the characteristic grain, not the arm grain."""
    assert _one(
        results_con,
        "SELECT count(*) FROM conformed.endpoint_results WHERE source_id = 'BL1'",
    ) == 1


def test_baseline_rows_are_not_given_a_timepoint_the_registry_never_wrote(results_con):
    """A baseline characteristic is measured at baseline by construction, but
    writing "Baseline" into the text the conformance engine reads would be the
    pipeline asserting a timepoint the trial did not state."""
    assert (
        _one(
            results_con,
            "SELECT time_frame_raw FROM conformed.endpoint_results WHERE source_id = 'BL1'",
        )
        is None
    )


# ---------------------------------------------------- D6: the dispersion table


def test_every_arm_level_measurement_lands_including_the_unusable_ones(results_con):
    """The denominator every aggregate reports against is only correct because
    nothing is filtered out here."""
    landed = _one(results_con, "SELECT count(*) FROM conformed.endpoint_dispersion")
    raw = _one(results_con, "SELECT count(*) FROM raw.outcome_measurements") + _one(
        results_con, "SELECT count(*) FROM raw.baseline_measurements"
    )
    assert landed == raw


def test_a_reported_sd_is_carried_through_and_a_derived_one_is_flagged(results_con):
    reported = results_con.execute(
        """
        SELECT sd_estimate, sd_method, sd_is_derived, sd_is_approximate
        FROM conformed.endpoint_dispersion WHERE source_id = 'OM1' AND group_key = 'OG000'
        """
    ).fetchone()
    assert reported == (0.31, "reported", False, False)

    derived = results_con.execute(
        """
        SELECT sd_method, sd_is_derived, sd_is_approximate
        FROM conformed.endpoint_dispersion WHERE source_id = 'OM4'
        """
    ).fetchone()
    assert derived == ("from_standard_error", True, False)

    approximate = results_con.execute(
        """
        SELECT sd_method, sd_is_derived, sd_is_approximate
        FROM conformed.endpoint_dispersion WHERE source_id = 'OM7'
        """
    ).fetchone()
    assert approximate == ("from_inter_quartile_range", True, True)


def test_the_arm_n_used_is_recorded_along_with_where_it_came_from(results_con):
    row = results_con.execute(
        """
        SELECT n, n_source FROM conformed.endpoint_dispersion
        WHERE source_id = 'OM4'
        """
    ).fetchone()
    assert row == (100, "outcome_group")


def test_the_inputs_of_every_derived_estimate_are_stored(results_con):
    inputs = json.loads(
        _one(
            results_con,
            "SELECT sd_inputs FROM conformed.endpoint_dispersion WHERE source_id = 'OM4'",
        )
    )
    assert inputs["standard_error"] == 0.8
    assert inputs["n"] == 100
    assert inputs["dispersion_kind"] == "standard_error"


def test_litres_and_millilitres_pool_only_after_conversion(results_con):
    """Two trials reporting the same endpoint in different units. `sd_estimate`
    keeps each trial's own unit; `sd_estimate_si` is what makes them
    comparable, and it is a separate column precisely so pooling is a
    decision rather than an accident."""
    rows = dict(
        results_con.execute(
            """
            SELECT source_id, sd_estimate FROM conformed.endpoint_dispersion
            WHERE source_id IN ('OM1', 'OM6') AND group_key = 'OG000'
            """
        ).fetchall()
    )
    assert rows == {"OM1": 0.31, "OM6": 310.0}

    si = dict(
        results_con.execute(
            """
            SELECT source_id, round(sd_estimate_si, 6) FROM conformed.endpoint_dispersion
            WHERE source_id IN ('OM1', 'OM6') AND group_key = 'OG000'
            """
        ).fetchall()
    )
    assert si == {"OM1": 0.31, "OM6": 0.31}
    assert {
        row[0] for row in results_con.execute(
            "SELECT si_scale_id FROM conformed.endpoint_dispersion WHERE source_id IN ('OM1','OM6')"
        ).fetchall()
    } == {"litres"}


def test_the_unit_field_resolves_a_bare_symbol_the_prose_matcher_will_not(results_con):
    """matching.yaml sets `min_synonym_length: 2`, so the generic matcher
    never matches "L" inside a sentence. As the entire content of
    `unit_of_measure`, "L" is unambiguous, and results/units.py says so."""
    assert results_con.execute(
        """
        SELECT scale_id, scale_match_method FROM conformed.endpoint_dispersion
        WHERE unit_raw = 'L' LIMIT 1
        """
    ).fetchone() == ("litres", "exact")


def test_every_refusal_names_its_reason(results_con):
    reasons = dict(
        results_con.execute(
            """
            SELECT source_id, sd_skip_reason FROM conformed.endpoint_dispersion
            WHERE sd_estimate IS NULL
            """
        ).fetchall()
    )
    assert reasons["OM2"] == "confidence_interval_around_a_median"
    assert reasons["OM3"] == "param_type_not_continuous"
    assert reasons["OM5"] == "param_type_not_continuous"
    assert reasons["OM8"] == "dispersion_type_unrecognised"


def test_an_unrecognised_dispersion_type_keeps_its_raw_string(results_con):
    """So `results coverage` can list the exact spellings the vocabulary is
    missing, which is the only way the enumeration ever gets closed."""
    assert results_con.execute(
        """
        SELECT dispersion_type_raw, dispersion_kind FROM conformed.endpoint_dispersion
        WHERE source_id = 'OM8'
        """
    ).fetchone() == ("Bootstrap Spread", "unknown")


# ------------------------------------------------------------- preconditions


def test_running_without_a_results_section_says_so(tmp_path):
    from clinical_endpoints.db import connect

    con = connect(tmp_path / "warehouse.duckdb")
    with pytest.raises(NoResults, match="raw.outcome_measures"):
        run_results_conform(con)


def test_rerunning_is_a_refresh_not_an_append(results_warehouse_path, tmp_path):
    import shutil

    copy = tmp_path / "copy.duckdb"
    shutil.copy(results_warehouse_path, copy)
    con = duckdb.connect(str(copy))
    before = _one(con, "SELECT count(*) FROM conformed.endpoint_dispersion")
    run_results_conform(con)
    run_results_conform(con)
    assert _one(con, "SELECT count(*) FROM conformed.endpoint_dispersion") == before
    con.close()
