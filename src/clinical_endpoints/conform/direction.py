"""Direction derivation: directions.yaml's `direction = apply(form.direction_rule,
measurement)`. Direction is never matched from text directly -- it is computed
from the resolved form's `direction_rule` and the resolved measurement's
`default_direction` / `event_polarity`, with directions.yaml's
`event_polarity_cues` allowed to override the measurement's own polarity per
vocab/README.md decision #3 (RECIST hosts both "time to progression" and
"time to response" under one measurement id).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import duckdb


@dataclass(frozen=True)
class DirectionRules:
    event_polarity_cues: dict[str, tuple[re.Pattern, ...]]  # polarity -> patterns
    default_when_underivable: str
    form_direction_rule: dict[str, str]  # form_id -> direction_rule
    measurement_default_direction: dict[str, Optional[str]]
    measurement_event_polarity: dict[str, Optional[str]]
    measurement_domain: dict[str, Optional[str]]
    direction_by_ta: dict[tuple[str, str], str]  # (measurement_id, ta_id) -> direction_id


def load_direction_rules(con: duckdb.DuckDBPyConnection) -> DirectionRules:
    cues: dict[str, list[re.Pattern]] = {}
    for polarity, pattern in con.execute("SELECT polarity, pattern FROM vocab.event_polarity_cues").fetchall():
        cues.setdefault(polarity, []).append(re.compile(pattern, re.IGNORECASE))

    default_row = con.execute(
        "SELECT value FROM vocab.vocab_settings WHERE dimension = 'direction' AND setting = 'default_when_underivable'"
    ).fetchone()

    form_direction_rule = dict(con.execute("SELECT id, direction_rule FROM vocab.forms").fetchall())
    measurements = con.execute("SELECT id, default_direction, event_polarity, domain FROM vocab.measurements").fetchall()
    direction_by_ta = {
        (measurement_id, ta_id): direction_id
        for measurement_id, ta_id, direction_id in con.execute(
            "SELECT measurement_id, ta_id, direction_id FROM vocab.measurement_direction_by_ta"
        ).fetchall()
    }

    return DirectionRules(
        event_polarity_cues={k: tuple(v) for k, v in cues.items()},
        default_when_underivable=default_row[0] if default_row else "not_stated",
        form_direction_rule=form_direction_rule,
        measurement_default_direction={m[0]: m[1] for m in measurements},
        measurement_event_polarity={m[0]: m[2] for m in measurements},
        measurement_domain={m[0]: m[3] for m in measurements},
        direction_by_ta=direction_by_ta,
    )


def _event_polarity_from_cues(text: str, cues: dict[str, tuple[re.Pattern, ...]]) -> Optional[str]:
    # directions.yaml resolution order: an event cue is more specific than the
    # measurement's own default_polarity, so it is checked first.
    for polarity in ("benefit", "harm"):
        for pattern in cues.get(polarity, ()):
            if pattern.search(text):
                return polarity
    return None


@dataclass(frozen=True)
class DirectionResult:
    direction_id: str
    event_polarity_used: Optional[str]


def derive_direction(
    form_id: str,
    measurement_id: Optional[str],
    cue_text: str,
    rules: DirectionRules,
    *,
    ta_id: Optional[str] = None,
) -> DirectionResult:
    polarity = _event_polarity_from_cues(cue_text, rules.event_polarity_cues)
    if polarity is None and measurement_id:
        polarity = rules.measurement_event_polarity.get(measurement_id)

    direction_rule = rules.form_direction_rule.get(form_id)

    if direction_rule == "higher_count_better":
        return DirectionResult("increase_is_better", polarity)
    if direction_rule == "neutral":
        return DirectionResult("neutral", polarity)
    if direction_rule == "inherit_event_polarity":
        mapped = {"harm": "decrease_is_better", "benefit": "increase_is_better"}.get(polarity)
        return DirectionResult(mapped or rules.default_when_underivable, polarity)
    if direction_rule == "time_polarity":
        mapped = {"harm": "longer_is_better", "benefit": "shorter_is_better"}.get(polarity)
        return DirectionResult(mapped or rules.default_when_underivable, polarity)

    # inherit_measurement (and any other/unrecognised rule): measurement's
    # default_direction, with a per-TA override where measurements.yaml declares
    # one (direction genuinely depends on indication -- weight in obesity vs
    # cachexia).
    if measurement_id and ta_id and (measurement_id, ta_id) in rules.direction_by_ta:
        return DirectionResult(rules.direction_by_ta[(measurement_id, ta_id)], polarity)
    default_direction = rules.measurement_default_direction.get(measurement_id) if measurement_id else None
    return DirectionResult(default_direction or rules.default_when_underivable, polarity)
