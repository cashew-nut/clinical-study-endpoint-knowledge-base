"""D9's vocabulary: what kind of effect an analysis reported, and what
non-inferiority margin it used.

`raw.outcome_analyses.param_type` mixes ratio-scale effects (hazard, odds and
risk ratios) with difference-scale ones (mean and median differences, risk
differences) and a tail of method-specific labels. Folding them is the same
open-set recognition `results/dispersion.py` does, for the same reason: the
value set could not be confirmed from this environment, so an unrecognised
label lands as `other` with its raw string intact rather than being forced
into a bucket.

The non-inferiority margin is the scarcer thing. Neither source has a column
for it: AACT carries `non_inferiority_type` plus a free-text
`non_inferiority_description`, and the API the same pair. The margin, when it
is stated at all, is stated in that prose. `parse_ni_margin` reads it
conservatively and records that it did -- a margin is a regulatory commitment,
so a guessed one is worse than none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from clinical_endpoints.results.dispersion import fold

#: effect kind -> markers, matched against the folded `param_type` padded with
#: underscores (see results/dispersion.py's `classify_dispersion`).
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

#: The kinds whose null value is 1 rather than 0. Reported so an aggregate can
#: say which side of the null a distribution sits on without the caller having
#: to know the convention for each label.
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
    #: 'description' when read out of the prose, None when nothing was read.
    source: Optional[str]
    #: The sentence it was read from, kept whether or not a number came out of
    #: it -- a margin nobody could parse is still evidence of what the trial
    #: committed to, and a reviewer can read it.
    text: Optional[str]


# "margin of -0.10 L", "NI margin: 10%", "non-inferiority margin of 1.3",
# "margin was -100 mL". The number must follow the word `margin` within a short
# window: a description mentioning a margin and separately quoting the observed
# effect must not have the effect read as the margin.
_MARGIN_RE = re.compile(
    r"margin[^0-9+\-]{0,24}(?P<sign>[+-])?\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>%|[A-Za-z/·µ]{1,12})?",
    re.IGNORECASE,
)


def parse_ni_margin(description: Optional[str]) -> NiMargin:
    """The non-inferiority margin stated in an analysis's free-text description.

    Deliberately narrow. It reads a number only where the word "margin"
    introduces it, refuses when the description names two candidate numbers
    after that word, and records nothing rather than a guess when neither
    holds. A distribution of NI margins per endpoint does not exist publicly
    in any form, which is exactly why the one built here must not contain
    numbers that were never margins.
    """
    if not description:
        return NiMargin(None, None, None, None)
    text = description.strip()
    matches = _MARGIN_RE.findall(text)
    if len(matches) != 1:
        # Nothing to read, or more than one candidate and no way to choose.
        return NiMargin(None, None, None, text)
    match = _MARGIN_RE.search(text)
    number = float(match.group("number"))
    if match.group("sign") == "-":
        number = -number
    unit = match.group("unit")
    if unit and unit.lower() in {"the", "of", "to", "and", "was", "is", "a", "in", "for"}:
        unit = None
    return NiMargin(number, unit, "description", text)
