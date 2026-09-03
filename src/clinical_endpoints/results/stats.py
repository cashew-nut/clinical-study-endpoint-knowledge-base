"""D7, D8 and D9: the empirical distributions the warehouse can now answer for.

`endpoints stats --measurement fev1` answers the question the whole results
tier exists for -- *what variability should I expect for this endpoint?* -- as
a distribution over the trials that actually reported one, never as a single
authoritative number.

Three rules shape every function here, and they are the reason the output
looks the way it does.

**Nothing pools across a boundary that changes the quantity.** Results are
grouped by (form, unit) before anything is summarised: the SD of FEV1 *change
from baseline* is not the SD of FEV1, and the SD in litres is not the SD in
millilitres. Where `scales.yaml` declares a conversion, the group is the
converted unit and the values are the converted ones; where it does not, each
unit stands alone. A log-scale SD (from a geometric CV) is never in the same
group as an arithmetic one.

**Every aggregate ships its denominator.** `coverage` on each group is the
share of conformed studies for that endpoint that reported a usable
dispersion. A `stats` output without it would be a machine for producing
confident numbers off eight arms.

**Baseline variability is its own source, not a fallback** (D8). For a
change-from-baseline endpoint the SD of the change score and the SD of the raw
baseline value are different quantities, and converting between them needs the
baseline/follow-up correlation, which registries do not report. `--source
baseline` selects the second; nothing silently substitutes it for the first.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
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
    """What to narrow the corpus to. `measurement` is the only one that is
    normally load-bearing; the rest exist because "the SD of FEV1" is not one
    number and the caller has to be able to say which one they mean."""

    measurement: Optional[str] = None
    form: Optional[str] = None
    scale: Optional[str] = None
    timepoint: Optional[str] = None
    ta: Optional[str] = None
    phase: Optional[str] = None
    source: str = "outcome"
    #: Wan et al.'s IQR/range estimators are approximations from order
    #: statistics. Included by default and always counted separately, so a
    #: library built mostly out of them is visibly that.
    include_approximate: bool = True
    #: `--only-reported` drops every derived SD, leaving only the ones trials
    #: reported as standard deviations outright.
    include_derived: bool = True


@dataclass
class SdGroup:
    """One (form, unit) group's distribution, and its denominator."""

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
    return " AND ".join(where), params


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


class NotComputed(RuntimeError):
    """`endpoints results conform` has not been run against this warehouse."""


def _require(con: duckdb.DuckDBPyConnection) -> None:
    for table in ("endpoint_results", "endpoint_dispersion"):
        if not _table_exists(con, "conformed", table):
            raise NotComputed(
                f"conformed.{table} is empty -- run `endpoints results conform` first "
                "(and `endpoints pull` without --no-results before that)"
            )


def sd_distribution(con: duckdb.DuckDBPyConnection, filters: StatsFilters) -> dict:
    """The arm-level SD distribution for the selected endpoint, one block per
    (form, unit) group, each with its own denominator."""
    _require(con)
    if filters.source not in SOURCES:
        raise ValueError(f"--source must be one of {SOURCES}, got {filters.source!r}")

    where, params = _filter_sql(filters)
    scale_filter = ""
    if filters.scale:
        scale_filter = " AND coalesce(d.si_scale_id, d.scale_id) = ?"

    # The pooling unit: the converted one where scales.yaml declares a
    # conversion, otherwise the reported one. Because si_scale_id is a
    # function of scale_id, a group never mixes converted and unconverted
    # values -- which is what makes one median per group defensible.
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

    # The denominator: conformed studies for this endpoint, whether or not
    # they reported a usable dispersion. A trial that reported none is absent
    # from the numerator and present here.
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
    """A linear-interpolation quantile, defined for a single value.

    `statistics.quantiles` needs at least two points and would raise on the
    perfectly ordinary case of one arm having reported a usable SD -- which is
    exactly the case whose number most needs its denominator printed beside
    it, not an exception.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def analysis_distribution(con: duckdb.DuckDBPyConnection, filters: StatsFilters) -> dict:
    """D9: the effect sizes, p-values and non-inferiority margins reported for
    the selected endpoint.

    Distributions, never a pooled estimate. Pooling a treatment effect across
    trials grouped only by conformed endpoint means pooling across different
    populations, comparators and eras -- that is a systematic review, not a
    query, and shipping it as a query invites exactly the misuse the audit
    trail exists to prevent.
    """
    _require(con)
    if not _table_exists(con, "raw", "outcome_analyses"):
        raise NotComputed("raw.outcome_analyses is empty -- run `endpoints pull` first")

    where, params = _filter_sql(StatsFilters(**{**filters.__dict__, "source": "outcome"}))
    # The unit the outcome was reported in, carried through so a difference-
    # scale effect is never pooled across units. Taken from the dispersion
    # table, which has already resolved it against scales.yaml.
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

    # A hazard, odds or risk ratio is dimensionless whatever the underlying
    # measurement was reported in, so those pool on the effect alone. Every
    # other effect is on the scale of the endpoint -- a mean difference of
    # 0.23 L and one of 120 mL are the same size, and their median is not a
    # number about anything.
    effects: dict[tuple, list] = {}
    effect_labels: dict[tuple, str] = {}
    for row in rows:
        kind = classify_effect(row[0])
        dimensionless = kind in RATIO_SCALE_EFFECTS
        key = (kind, None if dimensionless else row[15])
        if row[1] is not None:
            # A difference-scale effect is on the endpoint's own scale, so it
            # is converted alongside the unit it is grouped under -- a mean
            # difference of 120 mL and one of 0.23 L belong in one
            # distribution, at 0.12 and 0.23 litres, or in neither.
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

    # A censored p-value ("<0.001") carries its bound in `p_value_num`, so it
    # is counted but never treated as an observed value.
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
