"""The results section, shared by both ingestion backends (docs/ENDPOINT_RESULTS_SPEC.md, D4).

`raw.design_outcomes` carries what a study *planned* to measure. These five
tables carry what it *reported*: one row per reported outcome, per arm within
an outcome, per arm x class x category measurement, per statistical analysis,
and per baseline characteristic x arm.

Landing is thin, exactly as `ingest/design.py` is thin -- no conforming, no
unit normalisation, no dispersion arithmetic. Every enumerated field
(`param_type`, `dispersion_type`, `unit_of_measure`, the analysis
`param_type`) is stored **verbatim, as the registry wrote it**. That is not
laziness: this project's build environment cannot reach clinicaltrials.gov or
AACT (see the module docstring of `ingest/ctgov_api.py`, and
docs/ENDPOINT_RESULTS_SPEC.md's "What was not measured"), so the exact value
sets those fields use are not known here. A closed enum in the ingest layer
would silently drop the values it had not anticipated; a verbatim string
cannot. Folding those strings into the small set of kinds the arithmetic
needs happens once, downstream and auditably, in `results/dispersion.py`.

Both backends land the same shape, so everything downstream is source-agnostic
-- the property the README calls load-bearing. Where the two sources disagree
about spelling (PRIMARY vs Primary; STANDARD_DEVIATION vs Standard Deviation)
the row keeps its own source's spelling and the folding downstream is
case- and separator-insensitive.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

# --------------------------------------------------------------------- DDL

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

#: table name -> (DDL, column tuple), in landing order. Both backends iterate
#: this rather than repeating the list, so a table added here is landed by
#: both or by neither.
RESULTS_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("outcome_measures", OUTCOME_MEASURES_DDL, OUTCOME_MEASURES_COLUMNS),
    ("outcome_groups", OUTCOME_GROUPS_DDL, OUTCOME_GROUPS_COLUMNS),
    ("outcome_measurements", OUTCOME_MEASUREMENTS_DDL, OUTCOME_MEASUREMENTS_COLUMNS),
    ("outcome_analyses", OUTCOME_ANALYSES_DDL, OUTCOME_ANALYSES_COLUMNS),
    ("baseline_measurements", BASELINE_MEASUREMENTS_DDL, BASELINE_MEASUREMENTS_COLUMNS),
)

RESULTS_TABLE_NAMES: tuple[str, ...] = tuple(name for name, _ddl, _cols in RESULTS_TABLES)


# ------------------------------------------------------------------ id keys

def outcome_id(nct_id: str, outcome_type: Optional[str], title: Optional[str],
               time_frame: Optional[str], duplicate_ordinal: int = 0) -> str:
    """A stable content hash, not the source's own row id.

    Neither source offers a key this project can depend on: the CT.gov API's
    `outcomeMeasures[]` entries carry no id at all, and AACT's `outcomes.id`
    is a surrogate that is not stable across AACT's own rebuilds. A content
    hash makes re-pulling a study a refresh rather than a fresh identity,
    which is the same reason `conform/pipeline.py` hashes rather than uuids.

    `outcome_type` is case-folded into the key (but stored verbatim on the
    row) because the two backends spell the same value differently -- AACT's
    "Primary" and the API's "PRIMARY" name one outcome, and re-pulling a study
    through the other backend should not renumber it.

    `duplicate_ordinal` disambiguates the rare study that reports two outcomes
    identical in all four key fields; it is 0 for every other row.
    """
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
    """The *characteristic* grain -- one id per (study, baseline characteristic),
    shared by every arm row reporting it, so `conformed.endpoint_results` can
    link a characteristic once rather than once per arm."""
    key = "|".join([nct_id or "", (title or "").strip(), (unit_of_measure or "").strip()])
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def analysis_id(outcome_id_value: str, ordinal: int) -> str:
    return hashlib.md5(f"{outcome_id_value}|{ordinal}".encode("utf-8")).hexdigest()


def _trim_sql(expr: str) -> str:
    """`str.strip()`, in SQL. DuckDB's `trim()` strips spaces only, so a title
    ending in a newline would hash differently from the Python side and the
    same study pulled through the two backends would get two different
    `outcome_id`s -- the one thing these keys exist to prevent."""
    return f"regexp_replace(coalesce({expr}, ''), '^\\s+|\\s+$', '', 'g')"


def outcome_id_sql(
    nct_id: str, outcome_type: str, title: str, time_frame: str, duplicate_ordinal: str
) -> str:
    """`outcome_id` as a SQL expression over the AACT columns, so the AACT
    backend lands the same key the Python parser computes for the same study.
    Tested against `outcome_id` directly (tests/test_results_ingest.py) rather
    than trusted to stay in step by inspection."""
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
    """Hands out `outcome_id`s for one study, bumping `duplicate_ordinal` only
    when a later outcome collides with an earlier one on all four key fields."""

    def __init__(self) -> None:
        self._seen: dict[tuple, int] = {}

    def key(self, nct_id: str, outcome_type, title, time_frame) -> str:
        signature = (nct_id, (outcome_type or "").strip().lower(), (title or "").strip(),
                     (time_frame or "").strip())
        ordinal = self._seen.get(signature, 0)
        self._seen[signature] = ordinal + 1
        return outcome_id(nct_id, outcome_type, title, time_frame, ordinal)


# ------------------------------------------------------------- value parsing

# A registry numeric field is free text: "12.4", "-0.03", "1,204", "NA",
# "<0.001", "99.9%". Everything that is not a plain number keeps its string in
# the `*_value` column and leaves `*_value_num` NULL -- an unparseable value is
# a fact about the row, not a reason to drop it.
_NUMERIC_RE = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d*)(\.\d+)?([eE][+-]?\d+)?$")
_P_VALUE_RE = re.compile(r"^\s*(?P<modifier><=?|>=?|=|≤|≥|<|>)?\s*(?P<number>[0-9.eE+-]+)\s*$")


def to_number(value: Any) -> Optional[float]:
    """`value` as a float, or None if it is not a plain number. Thousands
    separators are tolerated; comparators, units and "NA" are not -- those
    belong to the verbatim column beside it."""
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

    The API reports one string ("<0.001"); AACT splits it into
    `p_value_modifier` + `p_value` already. Both end up in the same three
    columns, so a censored p-value keeps both halves and a distribution built
    over `p_value_num` can include or exclude the censored ones deliberately
    rather than by accident.

    `display` is the p-value as the registry shows it, canonicalised in one
    respect only: an equality is written without a redundant leading "=", so
    the API's "0.021" and AACT's ("=", 0.021) land the same string. `number`
    for a censored p-value is the *bound*, not the value -- 0.001 for
    "<0.001" -- which is why the modifier has to be read alongside it.
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
    """AACT's already-split (modifier, value) pair, put back through the same
    parser so both backends land identical columns."""
    modifier_text = (str(modifier).strip() if modifier is not None else "") or ""
    value_text = str(value).strip() if value is not None else ""
    if not value_text:
        return (modifier_text or None), (modifier_text or None), None
    return split_p_value(f"{modifier_text}{value_text}")


#: An analysis's `non_inferiority_type` value set is small but not knowable
#: from this environment (see the module docstring): observed values include
#: "Superiority", "Non-Inferiority", "Equivalence", "Non-Inferiority or
#: Equivalence" and "Superiority or Other". Matching on the substring rather
#: than an equality means an unanticipated spelling of the same idea is still
#: recognised, and anything genuinely new reads as "not a non-inferiority
#: analysis" rather than as one.
_NON_INFERIORITY_MARKERS = ("non-inferiority", "noninferiority", "equivalence")


def is_non_inferiority(flag: Any, type_text: Any) -> Optional[bool]:
    """Whether an analysis is a non-inferiority (or equivalence) comparison.

    The API states it outright; AACT states only the type string, so the
    string is the fallback -- and the source of truth for both when the flag
    is absent, so the two backends agree on rows where only one of them has a
    boolean to offer.
    """
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


# ------------------------------------------------- CT.gov API v2 extraction

def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _denominator_counts(denoms: Any) -> tuple[dict[str, Optional[int]], Optional[str]]:
    """`denoms[]` -> ({group_key: n}, units).

    A results module may state several denominators for one outcome (a
    participant count and an eye/lesion count, say). The participant one is
    preferred, because every conversion in `results/dispersion.py` that needs
    an `n` needs the number of *analysis units the statistic was computed
    over*, and the registry's own convention is that the first/participant
    denominator is that one. The units actually used are recorded beside the
    count rather than assumed.
    """
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
    """`classes[].categories[].measurements[]` -> flat rows, carrying the class
    and category titles down. A single-valued outcome reports exactly one
    class with one unnamed category, so the flattening is lossless."""
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
    """One study record from CT.gov API v2 -> rows for each of RESULTS_TABLES.

    `resultsSection` is already in the payload `ingest/ctgov_api.py` fetches --
    `_fetch_page` sends no `fields` parameter, so the full study record comes
    back and this module is the parsing that was missing. A study with no
    results section yields empty lists for every table, which is the ordinary
    case rather than an error.
    """
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
    """CT.gov's own `hasResults` flag, which is a claim about the registry
    record rather than about what this parser found -- a study can be flagged
    `hasResults` and still post only participant flow and adverse events.
    Landed so gate question 1 ("what share of conformed studies have results")
    is answerable against the registry's answer, not this project's."""
    return to_bool(study.get("hasResults"))
