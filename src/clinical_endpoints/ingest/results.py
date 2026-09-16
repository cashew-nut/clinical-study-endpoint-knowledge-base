"""The results section, shared by both backends: one row per reported
outcome, per arm within an outcome, per arm x class x category measurement,
per statistical analysis, and per baseline characteristic x arm.

Every enumerated field (`param_type`, `dispersion_type`, `unit_of_measure`)
is stored verbatim; the exact value sets are not known, and folding happens
downstream in `results/dispersion.py`, case- and separator-insensitively.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

OUTCOME_MEASURES_DDL = """
    outcome_id VARCHAR PRIMARY KEY,
    nct_id VARCHAR,
    ordinal INTEGER,
    outcome_type VARCHAR,
    title VARCHAR,
    description VARCHAR,
    time_frame VARCHAR,
    population VARCHAR,
    param_type VARCHAR,
    dispersion_type VARCHAR,
    unit_of_measure VARCHAR,
    units_analyzed VARCHAR,
    reporting_status VARCHAR
"""
OUTCOME_MEASURES_COLUMNS: tuple[str, ...] = (
    "outcome_id", "nct_id", "ordinal", "outcome_type", "title", "description",
    "time_frame", "population", "param_type", "dispersion_type", "unit_of_measure",
    "units_analyzed", "reporting_status",
)

OUTCOME_GROUPS_DDL = """
    outcome_id VARCHAR, nct_id VARCHAR, group_key VARCHAR, ordinal INTEGER,
    title VARCHAR, description VARCHAR, n INTEGER, n_units VARCHAR
"""
OUTCOME_GROUPS_COLUMNS: tuple[str, ...] = (
    "outcome_id", "nct_id", "group_key", "ordinal", "title", "description", "n", "n_units",
)

OUTCOME_MEASUREMENTS_DDL = """
    outcome_id VARCHAR, nct_id VARCHAR, group_key VARCHAR,
    class_title VARCHAR, category_title VARCHAR,
    param_value VARCHAR, param_value_num DOUBLE,
    dispersion_value VARCHAR, dispersion_value_num DOUBLE,
    dispersion_lower_limit DOUBLE, dispersion_upper_limit DOUBLE,
    n INTEGER, comment VARCHAR
"""
OUTCOME_MEASUREMENTS_COLUMNS: tuple[str, ...] = (
    "outcome_id", "nct_id", "group_key", "class_title", "category_title",
    "param_value", "param_value_num", "dispersion_value", "dispersion_value_num",
    "dispersion_lower_limit", "dispersion_upper_limit", "n", "comment",
)

OUTCOME_ANALYSES_DDL = """
    analysis_id VARCHAR PRIMARY KEY, outcome_id VARCHAR, nct_id VARCHAR, ordinal INTEGER,
    group_keys VARCHAR, group_description VARCHAR,
    param_type VARCHAR, param_value VARCHAR, param_value_num DOUBLE,
    dispersion_type VARCHAR, dispersion_value VARCHAR, dispersion_value_num DOUBLE,
    p_value VARCHAR, p_value_num DOUBLE, p_value_modifier VARCHAR, p_value_description VARCHAR,
    ci_percent DOUBLE, ci_n_sides VARCHAR, ci_lower_limit DOUBLE, ci_upper_limit DOUBLE,
    method VARCHAR, method_description VARCHAR,
    non_inferiority BOOLEAN, non_inferiority_type VARCHAR, non_inferiority_description VARCHAR,
    estimate_description VARCHAR, other_analysis_description VARCHAR
"""
OUTCOME_ANALYSES_COLUMNS: tuple[str, ...] = (
    "analysis_id", "outcome_id", "nct_id", "ordinal", "group_keys", "group_description",
    "param_type", "param_value", "param_value_num",
    "dispersion_type", "dispersion_value", "dispersion_value_num",
    "p_value", "p_value_num", "p_value_modifier", "p_value_description",
    "ci_percent", "ci_n_sides", "ci_lower_limit", "ci_upper_limit",
    "method", "method_description",
    "non_inferiority", "non_inferiority_type", "non_inferiority_description",
    "estimate_description", "other_analysis_description",
)

BASELINE_MEASUREMENTS_DDL = """
    baseline_id VARCHAR, nct_id VARCHAR, group_key VARCHAR, ordinal INTEGER,
    title VARCHAR, description VARCHAR, population VARCHAR,
    class_title VARCHAR, category_title VARCHAR,
    unit_of_measure VARCHAR, param_type VARCHAR,
    param_value VARCHAR, param_value_num DOUBLE,
    dispersion_type VARCHAR, dispersion_value VARCHAR, dispersion_value_num DOUBLE,
    dispersion_lower_limit DOUBLE, dispersion_upper_limit DOUBLE,
    n INTEGER, group_title VARCHAR
"""
BASELINE_MEASUREMENTS_COLUMNS: tuple[str, ...] = (
    "baseline_id", "nct_id", "group_key", "ordinal", "title", "description", "population",
    "class_title", "category_title", "unit_of_measure", "param_type",
    "param_value", "param_value_num", "dispersion_type", "dispersion_value",
    "dispersion_value_num", "dispersion_lower_limit", "dispersion_upper_limit", "n",
    "group_title",
)

RESULTS_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("outcome_measures", OUTCOME_MEASURES_DDL, OUTCOME_MEASURES_COLUMNS),
    ("outcome_groups", OUTCOME_GROUPS_DDL, OUTCOME_GROUPS_COLUMNS),
    ("outcome_measurements", OUTCOME_MEASUREMENTS_DDL, OUTCOME_MEASUREMENTS_COLUMNS),
    ("outcome_analyses", OUTCOME_ANALYSES_DDL, OUTCOME_ANALYSES_COLUMNS),
    ("baseline_measurements", BASELINE_MEASUREMENTS_DDL, BASELINE_MEASUREMENTS_COLUMNS),
)

RESULTS_TABLE_NAMES: tuple[str, ...] = tuple(name for name, _ddl, _cols in RESULTS_TABLES)


def outcome_id(nct_id: str, outcome_type: Optional[str], title: Optional[str],
               time_frame: Optional[str], duplicate_ordinal: int = 0) -> str:
    """A content hash: the API's `outcomeMeasures[]` carry no id and AACT's
    `outcomes.id` is not stable across rebuilds. `outcome_type` is case-folded
    into the key because the backends spell it differently.
    `duplicate_ordinal` disambiguates two outcomes identical in all four fields."""
    key = "|".join(
        [
            nct_id or "",
            (outcome_type or "").strip().lower(),
            (title or "").strip(),
            (time_frame or "").strip(),
            str(duplicate_ordinal),
        ]
    )
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def baseline_id(nct_id: str, title: Optional[str], unit_of_measure: Optional[str]) -> str:
    """One id per (study, baseline characteristic), shared by every arm row."""
    key = "|".join([nct_id or "", (title or "").strip(), (unit_of_measure or "").strip()])
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def analysis_id(outcome_id_value: str, ordinal: int) -> str:
    return hashlib.md5(f"{outcome_id_value}|{ordinal}".encode("utf-8")).hexdigest()


def _trim_sql(expr: str) -> str:
    """`str.strip()` in SQL; DuckDB's `trim()` strips spaces only."""
    return f"regexp_replace(coalesce({expr}, ''), '^\\s+|\\s+$', '', 'g')"


def outcome_id_sql(
    nct_id: str, outcome_type: str, title: str, time_frame: str, duplicate_ordinal: str
) -> str:
    """`outcome_id` as SQL over the AACT columns; tested against the Python version."""
    return (
        "md5("
        f"coalesce({nct_id}, '') || '|' || "
        f"lower({_trim_sql(outcome_type)}) || '|' || "
        f"{_trim_sql(title)} || '|' || "
        f"{_trim_sql(time_frame)} || '|' || "
        f"CAST({duplicate_ordinal} AS VARCHAR))"
    )


def baseline_id_sql(nct_id: str, title: str, unit_of_measure: str) -> str:
    return (
        "md5("
        f"coalesce({nct_id}, '') || '|' || "
        f"{_trim_sql(title)} || '|' || "
        f"{_trim_sql(unit_of_measure)})"
    )


def analysis_id_sql(outcome_id_expr: str, ordinal_expr: str) -> str:
    return f"md5({outcome_id_expr} || '|' || CAST({ordinal_expr} AS VARCHAR))"


class _OutcomeKeyer:
    """Hands out `outcome_id`s for one study, bumping `duplicate_ordinal` on collisions."""

    def __init__(self) -> None:
        self._seen: dict[tuple, int] = {}

    def key(self, nct_id: str, outcome_type, title, time_frame) -> str:
        signature = (nct_id, (outcome_type or "").strip().lower(), (title or "").strip(),
                     (time_frame or "").strip())
        ordinal = self._seen.get(signature, 0)
        self._seen[signature] = ordinal + 1
        return outcome_id(nct_id, outcome_type, title, time_frame, ordinal)


# A registry numeric field is free text: "12.4", "1,204", "NA", "<0.001",
# "99.9%". Anything not a plain number keeps its string in `*_value` and
# leaves `*_value_num` NULL.
_NUMERIC_RE = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d*)(\.\d+)?([eE][+-]?\d+)?$")
_P_VALUE_RE = re.compile(r"^\s*(?P<modifier><=?|>=?|=|≤|≥|<|>)?\s*(?P<number>[0-9.eE+-]+)\s*$")


def to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or not _NUMERIC_RE.match(text):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    number = to_number(value)
    if number is None:
        return None
    return int(number)


def split_p_value(value: Any) -> tuple[Optional[str], Optional[str], Optional[float]]:
    """A registry p-value -> (display, modifier, number).

    The API reports one string ("<0.001"); AACT splits it. `display` is the
    p-value as shown, with a redundant leading "=" dropped. `number` for a
    censored p-value is the bound, so the modifier has to be read alongside it.
    """
    if value is None:
        return None, None, None
    text = str(value).strip()
    if not text:
        return None, None, None
    match = _P_VALUE_RE.match(text)
    if not match:
        return text, None, None
    modifier = match.group("modifier")
    number = to_number(match.group("number"))
    if number is None:
        return text, modifier, None
    if modifier in (None, "=", ""):
        return match.group("number").strip(), "=", number
    return f"{modifier}{match.group('number').strip()}", modifier, number


def combine_p_value(modifier: Any, value: Any) -> tuple[Optional[str], Optional[str], Optional[float]]:
    """AACT's split (modifier, value) pair through the same parser."""
    modifier_text = (str(modifier).strip() if modifier is not None else "") or ""
    value_text = str(value).strip() if value is not None else ""
    if not value_text:
        return (modifier_text or None), (modifier_text or None), None
    return split_p_value(f"{modifier_text}{value_text}")


# Observed `non_inferiority_type` values include "Superiority",
# "Non-Inferiority", "Equivalence", "Non-Inferiority or Equivalence" and
# "Superiority or Other". Substring match so an unseen spelling still counts.
_NON_INFERIORITY_MARKERS = ("non-inferiority", "noninferiority", "equivalence")


def is_non_inferiority(flag: Any, type_text: Any) -> Optional[bool]:
    """The API states it outright; AACT states only the type string."""
    explicit = to_bool(flag)
    if explicit is not None:
        return explicit
    if type_text is None:
        return None
    token = str(type_text).strip().lower()
    if not token:
        return None
    return any(marker in token for marker in _NON_INFERIORITY_MARKERS)


def to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in {"true", "yes", "y", "1"}:
        return True
    if token in {"false", "no", "n", "0"}:
        return False
    return None


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _denominator_counts(denoms: Any) -> tuple[dict[str, Optional[int]], Optional[str]]:
    """`denoms[]` -> ({group_key: n}, units). The participant denominator is
    preferred where several are stated."""
    if not isinstance(denoms, list) or not denoms:
        return {}, None
    chosen = None
    for denom in denoms:
        units = (_get(denom, "units") or "").strip().lower()
        if "participant" in units:
            chosen = denom
            break
    if chosen is None:
        chosen = denoms[0]
    counts = {}
    for count in _get(chosen, "counts") or []:
        group_key = count.get("groupId")
        if group_key:
            counts[group_key] = to_int(count.get("value"))
    return counts, _get(chosen, "units")


def _measurement_rows(measure: dict, *, class_denoms: bool = True):
    """`classes[].categories[].measurements[]` -> flat rows with class and category titles."""
    for klass in _get(measure, "classes") or []:
        class_title = klass.get("title")
        class_counts, _units = (
            _denominator_counts(klass.get("denoms")) if class_denoms else ({}, None)
        )
        for category in _get(klass, "categories") or []:
            category_title = category.get("title")
            for measurement in _get(category, "measurements") or []:
                yield class_title, category_title, class_counts, measurement


def extract_ctgov_results(study: dict) -> dict[str, list[tuple]]:
    """One CT.gov API v2 study record -> rows for each of RESULTS_TABLES.
    A study with no results section yields empty lists."""
    nct_id = _get(study, "protocolSection", "identificationModule", "nctId")
    results = _get(study, "resultsSection") or {}
    rows: dict[str, list[tuple]] = {name: [] for name in RESULTS_TABLE_NAMES}
    if not nct_id or not results:
        return rows

    keyer = _OutcomeKeyer()
    for ordinal, measure in enumerate(
        _get(results, "outcomeMeasuresModule", "outcomeMeasures") or []
    ):
        outcome_key = keyer.key(
            nct_id, measure.get("type"), measure.get("title"), measure.get("timeFrame")
        )
        rows["outcome_measures"].append(
            (
                outcome_key, nct_id, ordinal, measure.get("type"), measure.get("title"),
                measure.get("description"), measure.get("timeFrame"),
                measure.get("populationDescription"), measure.get("paramType"),
                measure.get("dispersionType"), measure.get("unitOfMeasure"),
                measure.get("typeUnitsAnalyzed"), measure.get("reportingStatus"),
            )
        )

        group_counts, group_units = _denominator_counts(measure.get("denoms"))
        for group_ordinal, group in enumerate(measure.get("groups") or []):
            group_key = group.get("id")
            rows["outcome_groups"].append(
                (
                    outcome_key, nct_id, group_key, group_ordinal, group.get("title"),
                    group.get("description"), group_counts.get(group_key), group_units,
                )
            )

        for class_title, category_title, class_counts, measurement in _measurement_rows(measure):
            group_key = measurement.get("groupId")
            rows["outcome_measurements"].append(
                (
                    outcome_key, nct_id, group_key, class_title, category_title,
                    _as_text(measurement.get("value")), to_number(measurement.get("value")),
                    _as_text(measurement.get("spread")), to_number(measurement.get("spread")),
                    to_number(measurement.get("lowerLimit")),
                    to_number(measurement.get("upperLimit")),
                    class_counts.get(group_key), measurement.get("comment"),
                )
            )

        for analysis_ordinal, analysis in enumerate(measure.get("analyses") or []):
            verbatim, modifier, number = split_p_value(analysis.get("pValue"))
            rows["outcome_analyses"].append(
                (
                    analysis_id(outcome_key, analysis_ordinal), outcome_key, nct_id,
                    analysis_ordinal,
                    json.dumps(analysis.get("groupIds") or []),
                    analysis.get("groupDescription"),
                    analysis.get("paramType"), _as_text(analysis.get("paramValue")),
                    to_number(analysis.get("paramValue")),
                    analysis.get("dispersionType"), _as_text(analysis.get("dispersionValue")),
                    to_number(analysis.get("dispersionValue")),
                    verbatim, number, modifier, analysis.get("pValueComment"),
                    to_number(analysis.get("ciPctValue")), _as_text(analysis.get("ciNumSides")),
                    to_number(analysis.get("ciLowerLimit")), to_number(analysis.get("ciUpperLimit")),
                    analysis.get("statisticalMethod"), analysis.get("statisticalComment"),
                    is_non_inferiority(
                        analysis.get("nonInferiority"), analysis.get("nonInferiorityType")
                    ),
                    analysis.get("nonInferiorityType"), analysis.get("nonInferiorityComment"),
                    analysis.get("estimateComment"), analysis.get("otherAnalysisDescription"),
                )
            )

    baseline = _get(results, "baselineCharacteristicsModule") or {}
    baseline_group_titles = {
        group.get("id"): group.get("title") for group in baseline.get("groups") or []
    }
    module_counts, _module_units = _denominator_counts(baseline.get("denoms"))
    for ordinal, measure in enumerate(baseline.get("measures") or []):
        measure_counts, _units = _denominator_counts(measure.get("denoms"))
        characteristic = baseline_id(nct_id, measure.get("title"), measure.get("unitOfMeasure"))
        for class_title, category_title, class_counts, measurement in _measurement_rows(measure):
            group_key = measurement.get("groupId")
            n = (
                class_counts.get(group_key)
                or measure_counts.get(group_key)
                or module_counts.get(group_key)
            )
            rows["baseline_measurements"].append(
                (
                    characteristic, nct_id, group_key, ordinal, measure.get("title"),
                    measure.get("description"), measure.get("populationDescription"),
                    class_title, category_title, measure.get("unitOfMeasure"),
                    measure.get("paramType"),
                    _as_text(measurement.get("value")), to_number(measurement.get("value")),
                    measure.get("dispersionType"),
                    _as_text(measurement.get("spread")), to_number(measurement.get("spread")),
                    to_number(measurement.get("lowerLimit")),
                    to_number(measurement.get("upperLimit")),
                    n, baseline_group_titles.get(group_key),
                )
            )

    return rows


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def has_results(study: dict) -> Optional[bool]:
    """CT.gov's `hasResults` flag: a claim about the record, not about what
    this parser found. A flagged study may post only participant flow and
    adverse events."""
    return to_bool(study.get("hasResults"))
