"""The threshold comparator/value/unit parser.

Per vocab/README.md's "Known gaps": thresholds are parsed, not vocabularised --
forms.yaml only flags which forms `expects_threshold`, via the `expects_threshold`
column on vocab.forms. The comparator/value/unit regexes below are step-3
implementation, same as the plan intends; nothing here reads from a `threshold`
vocab file because there isn't one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_COMPARATOR_WORDS = {
    "≥": ">=", ">=": ">=", "at least": ">=", "no less than": ">=", "or more": ">=", "or better": ">=",
    "≤": "<=", "<=": "<=", "at most": "<=", "no more than": "<=", "or less": "<=", "or worse": "<=",
    ">": ">", "greater than": ">", "more than": ">", "above": ">",
    "<": "<", "less than": "<", "fewer than": "<", "below": "<",
    "=": "=", "exactly": "=",
}
_COMPARATOR_ALT = "|".join(sorted((re.escape(k) for k in _COMPARATOR_WORDS), key=len, reverse=True))

_EXPLICIT_RE = re.compile(
    rf"(?P<cmp>{_COMPARATOR_ALT})\s*(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>%|percent|points?|mmhg|mg/dl|mmol/l)?",
    re.IGNORECASE,
)
# "PASI75", "ACR20", "PASI 75": an ALL-CAPS instrument acronym directly followed
# by a 2-3 digit response threshold is, by registry convention, a ">=N%" response
# criterion (PASI75 = >=75% improvement in PASI). Restricted to all-caps acronyms
# so it does not fire on ordinary "Week 12" / "Day 90" style timepoints.
_ACRONYM_THRESHOLD_RE = re.compile(r"\b[A-Z]{2,6}\s?-?(\d{2,3})\b")


@dataclass(frozen=True)
class ThresholdResult:
    comparator: Optional[str]
    value: Optional[float]
    unit: Optional[str]


def parse_threshold(text: Optional[str]) -> ThresholdResult:
    if not text:
        return ThresholdResult(None, None, None)

    m = _EXPLICIT_RE.search(text)
    if m:
        comparator = _COMPARATOR_WORDS[m.group("cmp").lower()]
        unit = m.group("unit")
        if unit and unit.lower().startswith("percent"):
            unit = "%"
        return ThresholdResult(comparator, float(m.group("val")), unit)

    m = _ACRONYM_THRESHOLD_RE.search(text)
    if m:
        return ThresholdResult(">=", float(m.group(1)), "%")

    return ThresholdResult(None, None, None)
