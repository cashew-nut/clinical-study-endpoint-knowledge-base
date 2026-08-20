"""Tag values: what a `{tag}` renders to, and where its reference points.

Seven tags, three kinds of USDM home (see docs/USDM_ENDPOINTS_API_SPEC.md,
"Tag catalogue"):

* `measurement` / `concept` -> a `BiomedicalConceptSurrogate` on the study
  version, shared by every endpoint in the trial that resolved that term. This
  is the one that matters: it is the cross-study join key.
* `reference` / `timepoint` / `threshold` / `scale` -> an `ExtensionAttribute`
  on the endpoint itself, because USDM has no class for "the baseline this is
  measured against" or "Week 16" as a bare horizon.

Nothing here re-parses registry text. Every value comes from a column `conform`
already wrote.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: Comparators as `conform` records them, rendered for a human sentence.
_COMPARATOR_SYMBOLS = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "=": ""}

#: Units whose symbol attaches to the number without a space.
_TIGHT_UNITS = frozenset({"%"})

_PLURAL_UNITS = re.compile(r"s$", re.IGNORECASE)


#: `time_frame` strings that already open with a preposition need no help; the
#: rest are durations ("12 months") that read as a dangling noun without one.
_LEADING_PREPOSITIONS = frozenset(
    {"up", "from", "through", "during", "at", "over", "within", "until", "after",
     "before", "post", "pre", "throughout", "every", "each"}
)

#: A `time_frame` opening with a unit label ("Week 12, Week 24", "Baseline and
#: Day 28") names points in time, so it takes "at"; anything else is read as a
#: duration and takes "over".
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
    """`week` / `weeks` -> `Week`. The registry writes "Week 16", not "week 16"."""
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
    """Render 16.0 as "16" -- a visit number is not a float to a reader."""
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
    """Render `timepoint_pattern` + `timepoint_extracted` into a phrase.

    `timepoint_patterns.yaml` already declares, per pattern, the named capture
    groups a parser should populate -- it calls itself the parser spec rather
    than a classifier -- so this is a lookup, not an inference. Where the
    extracted fields are too thin to render, the raw `time_frame` string is used
    verbatim: less pretty, still true.

    **The rendered phrase carries its own preposition** ("at Week 16", "over 6
    weeks", "until the required number of events"), and templates therefore
    write a bare `[ {timepoint}]` with no preposition of their own. The
    alternative -- a preposition in the template -- produced "during through
    Week 52" and "up to until the required number of events", because a third
    of the patterns render a self-contained phrase rather than a point in time.
    Which preposition a category takes is a property of the category, so it
    belongs with the category.
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
        # The cap is a duration ("up to 36 months"), not a landmark visit
        # ("Month 36") -- an event-driven endpoint has no scheduled horizon.
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
    """One resolved tag: what it reads as, and what its ParameterMap points at.

    `host` is one of `surrogate` (a shared BiomedicalConceptSurrogate),
    `analysis_population` (a shared AnalysisPopulation) or `extension` (an
    ExtensionAttribute minted on the endpoint). `key` identifies the shared
    instance for the first two.
    """

    name: str
    value: str
    host: str
    key: str | None = None


#: Which host each tag uses. Adding a tag means giving its value a USDM home,
#: which is why vocab/schema.py keeps USDM_TAGS closed.
TAG_HOSTS: dict[str, str] = {
    "measurement": "surrogate",
    "concept": "surrogate",
    "reference": "extension",
    "timepoint": "extension",
    "threshold": "extension",
    "scale": "extension",
}

HOST_REFERENCE_ATTRIBUTE: dict[str, tuple[str, str]] = {
    "surrogate": ("BiomedicalConceptSurrogate", "label"),
    "extension": ("ExtensionAttribute", "valueString"),
}
