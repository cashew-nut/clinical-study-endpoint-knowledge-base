"""`endpoints stats`: the empirical distributions the results section supports.

Results are grouped by (form, unit) before anything is summarised, since the
SD of a change from baseline is not the SD of a raw value and litres are not
millilitres. Where scales.yaml declares a conversion, the group is the
converted unit. A log-scale SD is never grouped with an arithmetic one. Every
aggregate ships its denominator. Baseline variability is its own source
(`--source baseline`), never a fallback for the change-score SD.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from typing import Optional

import duckdb

from clinical_endpoints.results.effects import (
    RATIO_SCALE_EFFECTS,
    classify_effect,
    null_value,
    parse_ni_margin,
)

SOURCES = ("outcome", "baseline")


@dataclass(frozen=True)
class StatsFilters:
    measurement: Optional[str] = None
    form: Optional[str] = None
    scale: Optional[str] = None
    timepoint: Optional[str] = None
    ta: Optional[str] = None
    phase: Optional[str] = None
    # Always the study tier (conformed.study_drug_class), never the arm tier.
    drug_class: Optional[str] = None
    source: str = "outcome"
    include_approximate: bool = True
    include_derived: bool = True


@dataclass
class SdGroup:
    form_id: Optional[str]
    scale_id: Optional[str]
    sd_scale: str
    converted: bool
    studies: int = 0
    arms: int = 0
    participants: Optional[int] = None
    median: Optional[float] = None
    q1: Optional[float] = None
    q3: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    methods: dict = field(default_factory=dict)
    timepoints: list = field(default_factory=list)
    studies_conformed: int = 0

    @property
    def coverage(self) -> Optional[float]:
        if not self.studies_conformed:
            return None
        return self.studies / self.studies_conformed


def _filter_sql(filters: StatsFilters) -> tuple[str, list]:
    where = ["r.result_kind = ?"]
    params: list = [filters.source]
    if filters.measurement:
        where.append("r.measurement_id = ?")
        params.append(filters.measurement)
    if filters.form:
        where.append("r.form_id = ?")
        params.append(filters.form)
    if filters.timepoint:
        where.append("r.timepoint_pattern = ?")
        params.append(filters.timepoint)
    if filters.ta:
        where.append(
            "r.nct_id IN (SELECT nct_id FROM conformed.study_therapeutic_area "
            "WHERE ta_id = ? AND is_primary)"
        )
        params.append(filters.ta)
    if filters.phase:
        where.append("r.nct_id IN (SELECT nct_id FROM raw.studies WHERE phase = ?)")
        params.append(filters.phase)
    if filters.drug_class:
        # Study tier: the results section's group_key links to a protocol arm
        # only by title, so an arm-level join is not yet defensible.
        where.append(
            "r.nct_id IN (SELECT nct_id FROM conformed.study_drug_class WHERE drug_class_id = ?)"
        )
        params.append(filters.drug_class)
    return " AND ".join(where), params


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


class NotComputed(RuntimeError):
    pass


def _require(con: duckdb.DuckDBPyConnection, filters: Optional[StatsFilters] = None) -> None:
    for table in ("endpoint_results", "endpoint_dispersion"):
        if not _table_exists(con, "conformed", table):
            raise NotComputed(
                f"conformed.{table} is empty -- run `endpoints results conform` first "
                "(and `endpoints pull` without --no-results before that)"
            )
    if filters is not None and filters.drug_class and not _table_exists(
        con, "conformed", "study_drug_class"
    ):
        raise NotComputed(
            "conformed.study_drug_class is missing, so --drug-class has nothing to filter on "
            "-- run `endpoints vocab validate` and `endpoints pull` first"
        )


def sd_distribution(con: duckdb.DuckDBPyConnection, filters: StatsFilters) -> dict:
    """The arm-level SD distribution, one block per (form, unit) group."""
    _require(con, filters)
    if filters.source not in SOURCES:
        raise ValueError(f"--source must be one of {SOURCES}, got {filters.source!r}")

    where, params = _filter_sql(filters)
    scale_filter = ""
    if filters.scale:
        scale_filter = " AND coalesce(d.si_scale_id, d.scale_id) = ?"

    # si_scale_id is a function of scale_id, so a group never mixes converted
    # and unconverted values.
    rows = con.execute(
        f"""
        SELECT
            r.form_id,
            coalesce(d.si_scale_id, d.scale_id) AS pool_scale_id,
            d.si_scale_id IS NOT NULL AS converted,
            d.sd_scale,
            coalesce(d.sd_estimate_si, d.sd_estimate) AS sd,
            d.sd_method, d.sd_is_approximate, d.sd_is_derived,
            d.n, r.nct_id, r.timepoint_pattern
        FROM conformed.endpoint_dispersion d
        JOIN conformed.endpoint_results r ON r.result_id = d.result_id
        WHERE {where} AND d.sd_estimate IS NOT NULL{scale_filter}
        """,
        params + ([filters.scale] if filters.scale else []),
    ).fetchall()

    if not filters.include_approximate:
        rows = [row for row in rows if not row[6]]
    if not filters.include_derived:
        rows = [row for row in rows if not row[7]]

    denominator_rows = con.execute(
        f"""
        SELECT r.form_id, count(DISTINCT r.nct_id)
        FROM conformed.endpoint_results r
        WHERE {where}
        GROUP BY 1
        """,
        params,
    ).fetchall()
    conformed_by_form = dict(denominator_rows)
    conformed_studies = con.execute(
        f"SELECT count(DISTINCT r.nct_id) FROM conformed.endpoint_results r WHERE {where}",
        params,
    ).fetchone()[0]

    grouped: dict[tuple, list] = {}
    for row in rows:
        grouped.setdefault((row[0], row[1], row[2], row[3]), []).append(row)

    groups: list[SdGroup] = []
    for (form_id, scale_id, converted, sd_scale), members in grouped.items():
        values = sorted(row[4] for row in members)
        studies = {row[9] for row in members}
        ns = [row[8] for row in members if row[8] is not None]
        methods: dict[str, int] = {}
        for row in members:
            methods[row[5]] = methods.get(row[5], 0) + 1
        timepoints: dict[Optional[str], set] = {}
        for row in members:
            timepoints.setdefault(row[10], set()).add(row[9])

        groups.append(
            SdGroup(
                form_id=form_id,
                scale_id=scale_id,
                sd_scale=sd_scale,
                converted=bool(converted),
                studies=len(studies),
                arms=len(members),
                participants=sum(ns) if ns else None,
                median=statistics.median(values),
                q1=_quantile(values, 0.25),
                q3=_quantile(values, 0.75),
                minimum=values[0],
                maximum=values[-1],
                methods=dict(sorted(methods.items(), key=lambda kv: (-kv[1], kv[0]))),
                timepoints=sorted(
                    ((pattern, len(ncts)) for pattern, ncts in timepoints.items()),
                    key=lambda kv: (-kv[1], kv[0] or ""),
                ),
                studies_conformed=conformed_by_form.get(form_id, 0),
            )
        )
    groups.sort(key=lambda g: (-g.arms, g.form_id or "", g.scale_id or ""))

    skips = dict(
        con.execute(
            f"""
            SELECT d.sd_skip_reason, count(*)
            FROM conformed.endpoint_dispersion d
            JOIN conformed.endpoint_results r ON r.result_id = d.result_id
            WHERE {where} AND d.sd_estimate IS NULL
            GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ).fetchall()
    )

    return {
        "filters": filters,
        "groups": groups,
        "studies_conformed": conformed_studies,
        "studies_with_sd": len({row[9] for row in rows}),
        "skip_reasons": skips,
    }


def _quantile(values: list[float], q: float) -> Optional[float]:
    """Linear-interpolation quantile that is defined for a single value
    (`statistics.quantiles` raises below two points)."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


MAX_STRATA = 8


def stratify_by_drug_class(
    con: duckdb.DuckDBPyConnection,
    filters: StatsFilters,
    *,
    kind: str = "mechanism",
    analyses: bool = False,
    limit: int = MAX_STRATA,
) -> list[tuple[str, dict]]:
    """Run the selected distribution once per drug class in the filtered
    corpus, most-studied first. Returns [(drug_class_id, report), ...].

    `kind` defaults to `mechanism`; stratifying across kinds would put
    "PD-1 inhibitor" and "monoclonal antibody" in adjacent blocks as if they
    were alternatives.
    """
    _require(con)
    if not _table_exists(con, "conformed", "study_drug_class"):
        raise NotComputed(
            "conformed.study_drug_class is missing -- run `endpoints vocab validate` and "
            "`endpoints pull` before stratifying by drug class"
        )

    where, params = _filter_sql(filters)
    class_rows = con.execute(
        f"""
        SELECT c.drug_class_id, count(DISTINCT r.nct_id) AS studies
        FROM conformed.endpoint_results r
        JOIN conformed.study_drug_class c ON c.nct_id = r.nct_id
        WHERE {where} AND c.kind = ?
        GROUP BY 1 ORDER BY studies DESC, c.drug_class_id
        LIMIT ?
        """,
        params + [kind, max(limit, 1)],
    ).fetchall()

    compute = analysis_distribution if analyses else sd_distribution
    out: list[tuple[str, dict]] = []
    for drug_class_id, _studies in class_rows:
        stratum = replace(filters, drug_class=drug_class_id)
        out.append((drug_class_id, compute(con, stratum)))
    return out


def analysis_distribution(con: duckdb.DuckDBPyConnection, filters: StatsFilters) -> dict:
    """Effect sizes, p-values and non-inferiority margins reported for the
    selected endpoint. Distributions, never a pooled estimate."""
    _require(con, filters)
    if not _table_exists(con, "raw", "outcome_analyses"):
        raise NotComputed("raw.outcome_analyses is empty -- run `endpoints pull` first")

    where, params = _filter_sql(StatsFilters(**{**filters.__dict__, "source": "outcome"}))
    rows = con.execute(
        f"""
        SELECT
            a.param_type, a.param_value_num, a.p_value, a.p_value_num, a.p_value_modifier,
            a.ci_percent, a.ci_lower_limit, a.ci_upper_limit, a.method,
            a.non_inferiority, a.non_inferiority_type, a.non_inferiority_description,
            r.nct_id, r.form_id, a.group_description, u.pool_scale_id, u.unit_factor
        FROM raw.outcome_analyses a
        JOIN conformed.endpoint_results r ON r.source_id = a.outcome_id AND r.result_kind = 'outcome'
        LEFT JOIN (
            SELECT d.result_id,
                   min(coalesce(d.si_scale_id, d.scale_id)) AS pool_scale_id,
                   min(CASE WHEN d.si_scale_id IS NOT NULL
                            THEN TRY_CAST(sc.factor_to_si AS DOUBLE) ELSE 1.0 END) AS unit_factor
            FROM conformed.endpoint_dispersion d
            LEFT JOIN vocab.scales sc ON sc.id = d.scale_id
            GROUP BY d.result_id
        ) u ON u.result_id = r.result_id
        WHERE {where}
        """,
        params,
    ).fetchall()

    # Ratio effects are dimensionless and pool on the effect alone; every
    # other effect is on the endpoint's scale and pools per unit, converted
    # alongside it.
    effects: dict[tuple, list] = {}
    effect_labels: dict[tuple, str] = {}
    for row in rows:
        kind = classify_effect(row[0])
        dimensionless = kind in RATIO_SCALE_EFFECTS
        key = (kind, None if dimensionless else row[15])
        if row[1] is not None:
            factor = 1.0 if dimensionless else (row[16] if row[16] is not None else 1.0)
            effects.setdefault(key, []).append((row[1] * factor, row[12]))
        effect_labels.setdefault(key, row[0] or kind)

    effect_summary = []
    for (kind, scale_id), values in sorted(effects.items(), key=lambda kv: -len(kv[1])):
        numbers = sorted(v for v, _nct in values)
        effect_summary.append(
            {
                "effect_kind": kind,
                "scale_id": scale_id,
                "label": effect_labels.get((kind, scale_id)),
                "null_value": null_value(kind),
                "analyses": len(numbers),
                "studies": len({nct for _v, nct in values}),
                "median": statistics.median(numbers),
                "q1": _quantile(numbers, 0.25),
                "q3": _quantile(numbers, 0.75),
                "minimum": numbers[0],
                "maximum": numbers[-1],
            }
        )

    # A censored p-value ("<0.001") carries its bound in p_value_num; counted, never treated as observed.
    stated = [row for row in rows if row[2] is not None]
    censored = [row for row in stated if row[4] not in (None, "=")]
    exact = [row for row in stated if row[4] in (None, "=") and row[3] is not None]
    below = [row for row in stated if row[3] is not None and row[3] < 0.05]

    ni = []
    for row in rows:
        if not row[9]:
            continue
        margin = parse_ni_margin(row[11])
        ni.append(
            {
                "nct_id": row[12],
                "param_type": row[0],
                "non_inferiority_type": row[10],
                "margin": margin.value,
                "margin_unit": margin.unit,
                "margin_source": margin.source,
                "description": margin.text,
                "groups": row[14],
            }
        )

    studies_conformed = con.execute(
        f"SELECT count(DISTINCT r.nct_id) FROM conformed.endpoint_results r WHERE {where}",
        params,
    ).fetchone()[0]

    return {
        "filters": filters,
        "analyses": len(rows),
        "studies_with_analyses": len({row[12] for row in rows}),
        "studies_conformed": studies_conformed,
        "effects": effect_summary,
        "p_values": {
            "stated": len(stated),
            "exact": len(exact),
            "censored": len(censored),
            "below_0_05": len(below),
        },
        "non_inferiority": ni,
    }
