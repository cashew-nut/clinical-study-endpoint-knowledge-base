"""Turn what a trial reported as spread into an estimated standard deviation,
recording how.

`dispersion_type` mixes SD, SE, IQR, full range, several CI widths and
geometric CV; `param_type` mixes mean, median, LS mean, geometric mean and
count types. Both are recognised from an open set: an unrecognised value
yields no SD and a `sd_skip_reason` naming it, never a coerced number. A row
with no usable dispersion stays in the denominator.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Optional

_NORMAL = NormalDist()

_FOLD_RE = re.compile(r"[^a-z0-9]+")


def fold(value: Optional[str]) -> str:
    """A registry enumeration value -> a comparable key. Absorbs the API's
    `STANDARD_DEVIATION` vs AACT's `Standard Deviation`."""
    if value is None:
        return ""
    return _FOLD_RE.sub("_", value.strip().lower()).strip("_")


# dispersion kind -> folded substrings, in precedence order. Matched against the
# folded value padded with underscores, so `_sd_` is a whole-token test.
_DISPERSION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Before `standard_deviation` and before the CI rule: "standard error of
    # the mean" must not read as a confidence interval.
    ("standard_error", ("standard_error", "std_error", "_se_", "_sem_")),
    ("standard_deviation", ("standard_deviation", "std_deviation", "_sd_", "stdev", "_std_")),
    ("confidence_interval", ("confidence_interval", "_ci_")),
    ("inter_quartile_range", ("inter_quartile", "interquartile", "_iqr_")),
    ("full_range", ("full_range", "range")),
    (
        "geometric_cv",
        ("geometric_coefficient_of_variation", "geometric_cv", "_gcv_"),
    ),
    ("coefficient_of_variation", ("coefficient_of_variation", "_cv_")),
)

# Values that positively state "no dispersion reported", as opposed to unrecognised.
_DISPERSION_NONE = frozenset({"", "not_applicable", "na", "n_a", "none", "not_reported"})

# `least_squares_mean` and `geometric_mean` are checked before the bare `mean` they contain.
_PARAM_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("geometric_least_squares_mean", ("geometric_least_squares_mean", "geometric_ls_mean")),
    ("least_squares_mean", ("least_squares_mean", "_ls_mean_", "lsmean", "adjusted_mean")),
    ("geometric_mean", ("geometric_mean", "_gmt_", "_gmc_")),
    ("median", ("median",)),
    ("mean", ("mean", "average")),
    ("count_of_participants", ("count_of_participants", "number_of_participants")),
    ("count_of_units", ("count_of_units", "number_of_units", "count_of_events")),
    ("number", ("number", "count", "_n_")),
)

# The only param kinds an arm-level SD is meaningful for.
CONTINUOUS_PARAM_KINDS = frozenset(
    {"mean", "least_squares_mean", "median", "geometric_mean", "geometric_least_squares_mean"}
)

# The kinds a mean-based CI conversion is valid for. A CI around a median is
# not (upper - lower) x sqrt(n) / (2z) wide.
MEAN_LIKE_PARAM_KINDS = frozenset(
    {"mean", "least_squares_mean", "geometric_mean", "geometric_least_squares_mean"}
)

_CI_PERCENT_RE = re.compile(r"(\d{2,3}(?:[._]\d+)?)\s*(?:%|percent|pct)?")


def classify_dispersion(value: Optional[str]) -> str:
    folded = fold(value)
    if folded in _DISPERSION_NONE:
        return "none"
    padded = f"_{folded}_"
    for kind, markers in _DISPERSION_MARKERS:
        if any(marker in padded for marker in markers):
            return kind
    return "unknown"


def classify_param(value: Optional[str]) -> str:
    folded = fold(value)
    if not folded:
        return "unknown"
    padded = f"_{folded}_"
    for kind, markers in _PARAM_MARKERS:
        if any(marker in padded for marker in markers):
            return kind
    return "unknown"


def confidence_percent(value: Optional[str]) -> Optional[float]:
    """The stated confidence level of a CI dispersion type, e.g. 95.0 from
    "95% Confidence Interval". Parsed, not assumed: the registry carries 80,
    90, 95, 97.5 and 99, and assuming 95 for a 90% interval understates the SD by 18%."""
    folded = fold(value)
    if "confidence" not in folded and not re.search(r"(^|_)ci($|_)", folded):
        return None
    match = _CI_PERCENT_RE.search(folded)
    if not match:
        return None
    percent = float(match.group(1).replace("_", "."))
    if not 50.0 < percent < 100.0:
        return None
    return percent


def z_for_percent(percent: float) -> float:
    return _NORMAL.inv_cdf(1.0 - (1.0 - percent / 100.0) / 2.0)


def wan_iqr_divisor(n: int) -> float:
    """eta(n) = 2 x Phi^-1((0.75n - 0.125) / (n + 0.25)), Wan et al. (2014)
    method 3. Tends to 1.35 for large n."""
    return 2.0 * _NORMAL.inv_cdf((0.75 * n - 0.125) / (n + 0.25))


def wan_range_divisor(n: int) -> float:
    """xi(n) = 2 x Phi^-1((n - 0.375) / (n + 0.25)), Wan et al. (2014) method 1."""
    return 2.0 * _NORMAL.inv_cdf((n - 0.375) / (n + 0.25))


@dataclass(frozen=True)
class SdEstimate:
    """One arm-level measurement's estimated SD and how it got there.
    `value is None` is the ordinary case, with `skip_reason` saying why."""

    value: Optional[float] = None
    # 'reported' | 'from_standard_error' | 'from_confidence_interval' |
    # 'from_inter_quartile_range' | 'from_full_range' | 'from_geometric_cv'
    method: Optional[str] = None
    is_derived: bool = False
    # Wan et al.'s estimators are approximations from order statistics.
    is_approximate: bool = False
    # 'arithmetic' | 'log'. A geometric CV yields a log-scale SD.
    scale: str = "arithmetic"
    inputs: dict = field(default_factory=dict)
    skip_reason: Optional[str] = None


def estimate_sd(
    *,
    param_type: Optional[str],
    dispersion_type: Optional[str],
    dispersion_value: Optional[float],
    lower_limit: Optional[float] = None,
    upper_limit: Optional[float] = None,
    n: Optional[int] = None,
) -> SdEstimate:
    """One arm-level measurement -> an SD estimate, or a reason there is none.

    | reported as            | conversion                            | needs      |
    |------------------------|---------------------------------------|------------|
    | standard deviation     | identity                              | --         |
    | standard error         | SE x sqrt(n)                          | n          |
    | k% confidence interval | (upper - lower) x sqrt(n) / (2 z_k)   | n, a mean  |
    | inter-quartile range   | Wan et al. (2014) eta(n)              | n          |
    | full range             | Wan et al. (2014) xi(n)               | n          |
    | geometric CV           | sqrt(ln(1 + (CV/100)^2)), log scale   | --         |
    """
    param_kind = classify_param(param_type)
    dispersion_kind = classify_dispersion(dispersion_type)
    inputs: dict = {"param_kind": param_kind, "dispersion_kind": dispersion_kind}

    if param_kind not in CONTINUOUS_PARAM_KINDS:
        reason = "param_type_not_continuous" if param_kind != "unknown" else "param_type_unrecognised"
        return SdEstimate(skip_reason=reason, inputs=inputs)
    if dispersion_kind == "none":
        return SdEstimate(skip_reason="no_dispersion_reported", inputs=inputs)
    if dispersion_kind == "unknown":
        return SdEstimate(skip_reason="dispersion_type_unrecognised", inputs=inputs)
    if dispersion_kind == "coefficient_of_variation":
        # An arithmetic CV needs the mean, which is a different column.
        return SdEstimate(skip_reason="dispersion_type_unsupported", inputs=inputs)

    if dispersion_kind == "standard_deviation":
        if dispersion_value is None:
            return SdEstimate(skip_reason="no_dispersion_value", inputs=inputs)
        return SdEstimate(
            value=abs(dispersion_value), method="reported", is_derived=False,
            inputs={**inputs, "sd": dispersion_value},
        )

    if dispersion_kind == "geometric_cv":
        if dispersion_value is None:
            return SdEstimate(skip_reason="no_dispersion_value", inputs=inputs)
        # Reported as a percentage. The SD stays on the log scale: converting
        # to arithmetic needs the geometric mean and a log-normality assumption.
        cv = dispersion_value / 100.0
        return SdEstimate(
            value=math.sqrt(math.log(1.0 + cv * cv)), method="from_geometric_cv",
            is_derived=True, scale="log", inputs={**inputs, "geometric_cv_percent": dispersion_value},
        )

    if dispersion_kind == "standard_error":
        if dispersion_value is None:
            return SdEstimate(skip_reason="no_dispersion_value", inputs=inputs)
        if not _usable_n(n):
            return SdEstimate(skip_reason="no_arm_n", inputs=inputs)
        return SdEstimate(
            value=abs(dispersion_value) * math.sqrt(n), method="from_standard_error",
            is_derived=True, inputs={**inputs, "standard_error": dispersion_value, "n": n},
        )

    if dispersion_kind == "confidence_interval":
        if param_kind not in MEAN_LIKE_PARAM_KINDS:
            return SdEstimate(skip_reason="confidence_interval_around_a_median", inputs=inputs)
        width = _width(lower_limit, upper_limit)
        if width is None:
            return SdEstimate(skip_reason="no_interval_limits", inputs=inputs)
        if not _usable_n(n):
            return SdEstimate(skip_reason="no_arm_n", inputs=inputs)
        percent = confidence_percent(dispersion_type)
        if percent is None:
            return SdEstimate(skip_reason="confidence_level_unstated", inputs=inputs)
        z = z_for_percent(percent)
        return SdEstimate(
            value=width * math.sqrt(n) / (2.0 * z), method="from_confidence_interval",
            is_derived=True,
            inputs={**inputs, "width": width, "n": n, "confidence_percent": percent, "z": z},
        )

    if dispersion_kind in ("inter_quartile_range", "full_range"):
        width = _width(lower_limit, upper_limit)
        if width is None and dispersion_value is not None:
            # Some rows state the span as a single value rather than as limits.
            width = abs(dispersion_value)
        if width is None:
            return SdEstimate(skip_reason="no_interval_limits", inputs=inputs)
        if not _usable_n(n):
            return SdEstimate(skip_reason="no_arm_n", inputs=inputs)
        if dispersion_kind == "inter_quartile_range":
            divisor = wan_iqr_divisor(n)
            method = "from_inter_quartile_range"
        else:
            divisor = wan_range_divisor(n)
            method = "from_full_range"
        if divisor <= 0:
            return SdEstimate(skip_reason="no_arm_n", inputs=inputs)
        return SdEstimate(
            value=width / divisor, method=method, is_derived=True, is_approximate=True,
            inputs={**inputs, "width": width, "n": n, "divisor": divisor},
        )

    return SdEstimate(skip_reason="dispersion_type_unrecognised", inputs=inputs)


def _usable_n(n: Optional[int]) -> bool:
    return n is not None and n >= 2


def _width(lower: Optional[float], upper: Optional[float]) -> Optional[float]:
    if lower is None or upper is None:
        return None
    return abs(upper - lower)
