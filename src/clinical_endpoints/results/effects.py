"""What kind of effect an analysis reported, and its non-inferiority margin.

`param_type` is recognised from an open set, as in results/dispersion.py: an
unrecognised label lands as `other` with its raw string intact. The
non-inferiority margin has no column in either source; when stated at all it
is in the free-text description, and `parse_ni_margin` reads it conservatively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from clinical_endpoints.results.dispersion import fold

# effect kind -> markers, matched against the folded `param_type` padded with underscores.
_EFFECT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hazard_ratio", ("hazard_ratio", "_hr_", "cox_proportional_hazard")),
    ("odds_ratio", ("odds_ratio", "_or_")),
    ("risk_ratio", ("risk_ratio", "relative_risk", "_rr_")),
    ("risk_difference", ("risk_difference", "_rd_")),
    ("rate_ratio", ("rate_ratio", "incidence_rate_ratio")),
    ("mean_difference", ("mean_difference", "difference_in_means")),
    ("median_difference", ("median_difference",)),
    ("slope", ("slope",)),
)

# Kinds whose null value is 1 rather than 0.
RATIO_SCALE_EFFECTS = frozenset({"hazard_ratio", "odds_ratio", "risk_ratio", "rate_ratio"})


def classify_effect(param_type: Optional[str]) -> str:
    padded = f"_{fold(param_type)}_"
    if padded == "__":
        return "unknown"
    for kind, markers in _EFFECT_MARKERS:
        if any(marker in padded for marker in markers):
            return kind
    return "other"


def null_value(effect_kind: str) -> Optional[float]:
    if effect_kind in RATIO_SCALE_EFFECTS:
        return 1.0
    if effect_kind in ("mean_difference", "median_difference", "risk_difference", "slope"):
        return 0.0
    return None


@dataclass(frozen=True)
class NiMargin:
    value: Optional[float]
    unit: Optional[str]
    source: Optional[str]  # 'description' when read from the prose
    text: Optional[str]  # the description, kept whether or not a number was read


# "margin of -0.10 L", "NI margin: 10%", "margin was -100 mL". The number must
# follow "margin" within a short window so a separately quoted observed effect
# is not read as the margin.
_MARGIN_RE = re.compile(
    r"margin[^0-9+\-]{0,24}(?P<sign>[+-])?\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>%|[A-Za-z/·µ]{1,12})?",
    re.IGNORECASE,
)


def parse_ni_margin(description: Optional[str]) -> NiMargin:
    """Reads a number only where "margin" introduces it, and refuses when the
    description offers more than one candidate."""
    if not description:
        return NiMargin(None, None, None, None)
    text = description.strip()
    matches = _MARGIN_RE.findall(text)
    if len(matches) != 1:
        return NiMargin(None, None, None, text)
    match = _MARGIN_RE.search(text)
    number = float(match.group("number"))
    if match.group("sign") == "-":
        number = -number
    unit = match.group("unit")
    if unit and unit.lower() in {"the", "of", "to", "and", "was", "is", "a", "in", "for"}:
        unit = None
    return NiMargin(number, unit, "description", text)
