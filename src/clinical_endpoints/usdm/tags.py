"""Tag values: what a `{tag}` renders to, and where its reference points.

* `measurement` / `concept` / `event` -> a `BiomedicalConceptSurrogate` on the
  study version, shared by every endpoint in the trial that resolved that term.
* `summary` / `reference` / `timepoint` / `threshold` / `scale` -> an `ExtensionAttribute`
  on the endpoint, since USDM has no class for these.

Every value comes from a column `conform` already wrote.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_COMPARATOR_SYMBOLS = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "=": ""}

# Units whose symbol attaches to the number without a space.
_TIGHT_UNITS = frozenset({"%"})

_PLURAL_UNITS = re.compile(r"s$", re.IGNORECASE)


# A `time_frame` opening with one of these needs no preposition.
_LEADING_PREPOSITIONS = frozenset(
    {"up", "from", "through", "during", "at", "over", "within", "until", "after",
     "before", "post", "pre", "throughout", "every", "each"}
)

# A `time_frame` opening with a unit label names points in time and takes "at";
# anything else reads as a duration and takes "over".
_LEADING_UNITS = frozenset(
    {"baseline", "week", "weeks", "day", "days", "month", "months", "year", "years",
     "hour", "hours", "visit", "visits", "cycle", "cycles", "screening", "randomisation",
     "randomization", "end"}
)


def _fallback(raw_text: str | None) -> str | None:
    """The raw `time_frame`, made to read as a phrase inside a sentence."""
    if not raw_text:
        return None
    first = re.split(r"[\s,]+", raw_text, maxsplit=1)[0].strip(".,;:").lower()
    if first in _LEADING_PREPOSITIONS:
        return raw_text[:1].lower() + raw_text[1:]
    if first in _LEADING_UNITS:
        return f"at {raw_text[:1].lower() + raw_text[1:]}"
    return f"over {raw_text}"


def _titled_unit(unit: str | None) -> str | None:
    """`week` / `weeks` -> `Week`."""
    if not unit:
        return None
    token = str(unit).strip()
    if not token:
        return None
    token = _PLURAL_UNITS.sub("", token) if len(token) > 3 else token
    return token[:1].upper() + token[1:].lower()


def _plural_unit(unit: str | None) -> str | None:
    titled = _titled_unit(unit)
    return titled + "s" if titled else None


def _number(value) -> str:
    """Render 16.0 as "16"."""
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(as_float)) if as_float.is_integer() else str(as_float)


def render_threshold(comparator: str | None, value, unit: str | None) -> str | None:
    """`(">=", 75.0, "%")` -> `"≥75%"`."""
    if value is None:
        return None
    symbol = _COMPARATOR_SYMBOLS.get(comparator or "", "")
    number = _number(value)
    if unit:
        unit = unit.strip()
        number = f"{number}{unit}" if unit in _TIGHT_UNITS else f"{number} {unit}"
    return f"{symbol}{number}" if symbol else number


def render_timepoint(pattern: str | None, extracted: dict | str | None, raw: str | None) -> str | None:
    """Render `timepoint_pattern` + `timepoint_extracted` into a phrase, falling
    back to the raw `time_frame` where the extracted fields are too thin.

    The rendered phrase carries its own preposition ("at Week 16", "over 6
    weeks", "until the required number of events"); templates write a bare
    `[ {timepoint}]`. A preposition in the template produced "during through
    Week 52".
    """
    if pattern is None or pattern == "unspecified":
        return None
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except (TypeError, ValueError):
            extracted = {}
    fields = extracted or {}
    raw_text = " ".join((raw or "").split()) or None

    def at(value, unit) -> str | None:
        u, v = _titled_unit(unit), fields.get(value) if isinstance(value, str) else value
        return f"{u} {_number(v)}" if u and v is not None else None

    if pattern == "baseline_only":
        return "at baseline"

    if pattern == "single_fixed" or pattern == "baseline_to_timepoint":
        point = at("value", fields.get("unit"))
        return f"at {point}" if point else _fallback(raw_text)

    if pattern == "visit_window":
        base = at("value", fields.get("unit"))
        if not base:
            return _fallback(raw_text)
        window = fields.get("window_pm")
        return f"at {base} (±{_number(window)})" if window else f"at {base}"

    if pattern == "anchored_offset":
        start = at("value", fields.get("unit"))
        end = at("value_end", fields.get("unit"))
        anchor = fields.get("anchor")
        span = f"from {start} to {end}" if start and end else (f"at {start}" if start else None)
        if not span:
            return _fallback(raw_text)
        return f"{span} after {anchor}" if anchor else span

    if pattern == "multi_timepoint":
        values, unit = fields.get("values"), _plural_unit(fields.get("unit"))
        if values and unit:
            return f"at {unit} {' '.join(str(values).split())}"
        return _fallback(raw_text)

    if pattern == "bare_duration":
        value = fields.get("value") or fields.get("value_alt")
        unit = _plural_unit(fields.get("unit"))
        if value is None or not unit:
            return _fallback(raw_text)
        approx = "approximately " if fields.get("approximate") else ""
        return f"over {approx}{_number(value)} {unit.lower()}"

    if pattern == "cumulative_window":
        end = at("end_value", fields.get("end_unit"))
        start = fields.get("start_anchor")
        if end and start:
            return f"from {start} through {end}"
        return f"through {end}" if end else _fallback(raw_text)

    if pattern == "event_driven":
        # The cap is a duration ("up to 36 months"), not a landmark visit.
        value = fields.get("estimated_max_value")
        unit = _plural_unit(fields.get("estimated_max_unit"))
        base = "until the required number of events"
        if value is None or not unit:
            return base
        return f"{base} (up to {_number(value)} {unit.lower()})"

    if pattern == "event_relative":
        anchor = fields.get("anchor")
        return f"relative to {anchor}" if anchor else _fallback(raw_text)

    return _fallback(raw_text)


@dataclass(frozen=True)
class TagValue:
    """One resolved tag. `host` is `surrogate` or `extension`; `key`
    identifies the shared instance for the former."""

    name: str
    value: str
    host: str
    key: str | None = None


# Adding a tag means giving its value a USDM home, which is why
# vocab/schema.py keeps USDM_TAGS closed.
TAG_HOSTS: dict[str, str] = {
    "measurement": "surrogate",
    "concept": "surrogate",
    "event": "surrogate",
    "summary": "extension",
    "reference": "extension",
    "timepoint": "extension",
    "threshold": "extension",
    "scale": "extension",
}

HOST_REFERENCE_ATTRIBUTE: dict[str, tuple[str, str]] = {
    "surrogate": ("BiomedicalConceptSurrogate", "label"),
    "extension": ("ExtensionAttribute", "valueString"),
}
