"""D6, the arithmetic on its own: folding the registry's enumerations, and the
five conversions that turn a reported spread into an SD.

This is the piece the roadmap calls the one where an error is least visible
downstream, so the tests are about the refusals as much as the conversions.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from clinical_endpoints.results.dispersion import (
    classify_dispersion,
    classify_param,
    confidence_percent,
    estimate_sd,
    fold,
    wan_iqr_divisor,
    wan_range_divisor,
    z_for_percent,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("STANDARD_DEVIATION", "standard_deviation"),
        ("Standard Deviation", "standard_deviation"),
        ("standard deviation", "standard_deviation"),
        ("Standard Deviation (SD)", "standard_deviation"),
        ("STANDARD_ERROR", "standard_error"),
        ("Standard Error of the Mean", "standard_error"),
        ("95% Confidence Interval", "confidence_interval"),
        ("90%_CONFIDENCE_INTERVAL", "confidence_interval"),
        ("INTER_QUARTILE_RANGE", "inter_quartile_range"),
        ("Interquartile Range", "inter_quartile_range"),
        ("Full Range", "full_range"),
        ("Geometric Coefficient of Variation", "geometric_cv"),
        ("Not Applicable", "none"),
        (None, "none"),
        ("", "none"),
        ("Bootstrap Spread", "unknown"),
    ],
)
def test_dispersion_types_fold_across_both_backends_spellings(raw, expected):
    """The API writes STANDARD_DEVIATION and AACT writes Standard Deviation.
    Both are landed verbatim, so the folding has to absorb the difference --
    and has to leave a value it does not recognise as `unknown` rather than
    guessing at it."""
    assert classify_dispersion(raw) == expected


def test_standard_error_is_not_read_as_a_standard_deviation():
    """The single most consequential confusion in this module: an SE is
    roughly sqrt(n) times smaller than the SD, so reading one as the other
    understates variability by an order of magnitude on a large trial."""
    assert classify_dispersion("Standard Error") == "standard_error"
    assert classify_dispersion("SEM") == "standard_error"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MEAN", "mean"),
        ("Mean", "mean"),
        ("MEDIAN", "median"),
        ("LEAST_SQUARES_MEAN", "least_squares_mean"),
        ("Least Squares Mean", "least_squares_mean"),
        ("GEOMETRIC_MEAN", "geometric_mean"),
        ("Geometric Least Squares Mean", "geometric_least_squares_mean"),
        ("COUNT_OF_PARTICIPANTS", "count_of_participants"),
        ("Number", "number"),
        ("Sponsor's own statistic", "unknown"),
    ],
)
def test_param_types_fold(raw, expected):
    assert classify_param(raw) == expected


def test_fold_collapses_separators_and_case():
    assert fold("95%_CONFIDENCE_INTERVAL") == "95_confidence_interval"
    assert fold("  Inter-Quartile Range ") == "inter_quartile_range"


def test_confidence_percent_is_read_not_assumed():
    """A 90% interval is 18% narrower than a 95% one. Assuming 95 would
    understate every SD derived from a 90% interval by that much."""
    assert confidence_percent("95% Confidence Interval") == 95.0
    assert confidence_percent("90%_CONFIDENCE_INTERVAL") == 90.0
    assert confidence_percent("97.5% Confidence Interval") == 97.5
    assert confidence_percent("Standard Deviation") is None


def test_z_for_percent_matches_the_textbook_values():
    assert z_for_percent(95) == pytest.approx(1.959964, abs=1e-5)
    assert z_for_percent(90) == pytest.approx(1.644854, abs=1e-5)
    assert z_for_percent(99) == pytest.approx(2.575829, abs=1e-5)


# -------------------------------------------------------------- conversions


def test_reported_standard_deviation_is_carried_through_unchanged():
    result = estimate_sd(
        param_type="MEAN", dispersion_type="Standard Deviation", dispersion_value=0.31
    )
    assert result.value == 0.31
    assert result.method == "reported"
    assert result.is_derived is False
    assert result.is_approximate is False


def test_standard_error_becomes_a_standard_deviation_with_the_arm_n():
    result = estimate_sd(
        param_type="MEAN", dispersion_type="Standard Error", dispersion_value=0.8, n=100
    )
    assert result.value == pytest.approx(8.0)
    assert result.method == "from_standard_error"
    assert result.is_derived is True
    assert result.inputs["n"] == 100


def test_standard_error_without_an_arm_n_yields_nothing():
    """`SE x sqrt(n)` needs a trustworthy n. Without one the row is in the
    denominator and out of the numerator -- never imputed."""
    result = estimate_sd(
        param_type="MEAN", dispersion_type="Standard Error", dispersion_value=0.8, n=None
    )
    assert result.value is None
    assert result.skip_reason == "no_arm_n"


def test_confidence_interval_uses_the_stated_level():
    around_95 = estimate_sd(
        param_type="MEAN", dispersion_type="95% Confidence Interval",
        dispersion_value=None, lower_limit=0.1, upper_limit=0.5, n=100,
    )
    assert around_95.value == pytest.approx(0.4 * 10 / (2 * 1.959964), rel=1e-6)
    assert around_95.inputs["confidence_percent"] == 95.0

    around_90 = estimate_sd(
        param_type="MEAN", dispersion_type="90% Confidence Interval",
        dispersion_value=None, lower_limit=0.1, upper_limit=0.5, n=100,
    )
    # A narrower z means a *larger* implied SD for the same interval width.
    assert around_90.value > around_95.value


def test_a_confidence_interval_around_a_median_is_refused():
    """The width-to-SD formula inverts the standard error of a *mean*. A
    median's CI is not that quantity, and converting it anyway is the kind of
    error that is invisible in the output."""
    result = estimate_sd(
        param_type="MEDIAN", dispersion_type="95% Confidence Interval",
        dispersion_value=None, lower_limit=15.1, upper_limit=22.0, n=240,
    )
    assert result.value is None
    assert result.skip_reason == "confidence_interval_around_a_median"


def test_inter_quartile_range_uses_the_wan_estimator_and_is_flagged_approximate():
    n = 150
    result = estimate_sd(
        param_type="MEDIAN", dispersion_type="Inter-Quartile Range",
        dispersion_value=None, lower_limit=90.0, upper_limit=420.0, n=n,
    )
    assert result.value == pytest.approx(330.0 / wan_iqr_divisor(n))
    assert result.method == "from_inter_quartile_range"
    assert result.is_derived is True
    assert result.is_approximate is True


def test_full_range_uses_the_other_wan_estimator():
    n = 40
    result = estimate_sd(
        param_type="MEDIAN", dispersion_type="Full Range",
        dispersion_value=None, lower_limit=1.0, upper_limit=9.0, n=n,
    )
    assert result.value == pytest.approx(8.0 / wan_range_divisor(n))
    assert result.method == "from_full_range"
    assert result.is_approximate is True


def test_wan_divisors_match_their_published_formulae():
    for n in (5, 20, 150, 1000):
        assert wan_iqr_divisor(n) == pytest.approx(
            2 * NormalDist().inv_cdf((0.75 * n - 0.125) / (n + 0.25))
        )
        assert wan_range_divisor(n) == pytest.approx(
            2 * NormalDist().inv_cdf((n - 0.375) / (n + 0.25))
        )
    # eta(n) tends to the textbook 1.35 that this deliberately does not
    # hardcode for small samples.
    assert wan_iqr_divisor(100000) == pytest.approx(1.349, abs=0.005)
    assert wan_iqr_divisor(6) < 1.30


def test_geometric_cv_stays_on_the_log_scale():
    result = estimate_sd(
        param_type="GEOMETRIC_MEAN",
        dispersion_type="Geometric Coefficient of Variation",
        dispersion_value=45.0,
    )
    assert result.scale == "log"
    assert result.value == pytest.approx(math.sqrt(math.log(1 + 0.45**2)))
    assert result.method == "from_geometric_cv"


# ------------------------------------------------------------------ refusals


def test_a_count_typed_outcome_has_no_standard_deviation():
    """`Count of Participants` rows carry a dispersion column too. Pooling
    their spread into an SD library would put participant counts and litres in
    the same distribution."""
    result = estimate_sd(
        param_type="COUNT_OF_PARTICIPANTS", dispersion_type="Standard Deviation",
        dispersion_value=12.0, n=240,
    )
    assert result.value is None
    assert result.skip_reason == "param_type_not_continuous"


def test_an_unrecognised_dispersion_type_is_reported_not_guessed():
    result = estimate_sd(
        param_type="MEAN", dispersion_type="Bootstrap Spread", dispersion_value=45.0, n=150
    )
    assert result.value is None
    assert result.skip_reason == "dispersion_type_unrecognised"
    # The raw string is still on the row it came from, so `results coverage`
    # can list exactly which spellings the vocabulary is missing.
    assert result.inputs["dispersion_kind"] == "unknown"


def test_a_row_stating_no_dispersion_is_distinguished_from_one_we_failed_to_read():
    stated = estimate_sd(
        param_type="MEAN", dispersion_type="Not Applicable", dispersion_value=None
    )
    assert stated.skip_reason == "no_dispersion_reported"
    unread = estimate_sd(
        param_type="MEAN", dispersion_type="Bootstrap Spread", dispersion_value=1.0, n=10
    )
    assert unread.skip_reason == "dispersion_type_unrecognised"


def test_an_arm_of_one_is_not_enough_for_any_derived_estimate():
    for dispersion_type, kwargs in (
        ("Standard Error", {"dispersion_value": 1.0}),
        ("95% Confidence Interval", {"lower_limit": 1.0, "upper_limit": 2.0}),
        ("Inter-Quartile Range", {"lower_limit": 1.0, "upper_limit": 2.0}),
    ):
        result = estimate_sd(param_type="MEAN", dispersion_type=dispersion_type, n=1, **{
            "dispersion_value": None, **kwargs
        })
        assert result.skip_reason == "no_arm_n", dispersion_type


def test_every_estimate_records_how_it_was_derived():
    """A standing constraint: `sd_method`, `sd_is_derived` and the inputs are
    as non-negotiable as `match_method` and `confidence` are on the
    conformance side."""
    for result in (
        estimate_sd(param_type="MEAN", dispersion_type="Standard Deviation", dispersion_value=1.0),
        estimate_sd(param_type="MEAN", dispersion_type="Standard Error", dispersion_value=1.0, n=9),
        estimate_sd(
            param_type="MEAN", dispersion_type="95% Confidence Interval",
            lower_limit=1.0, upper_limit=2.0, n=9, dispersion_value=None,
        ),
        estimate_sd(
            param_type="MEDIAN", dispersion_type="Inter-Quartile Range",
            lower_limit=1.0, upper_limit=2.0, n=9, dispersion_value=None,
        ),
    ):
        assert result.value is not None
        assert result.method
        assert result.inputs["param_kind"]
        assert result.inputs["dispersion_kind"]
