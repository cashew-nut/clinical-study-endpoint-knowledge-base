"""Pull filters shared by both ingestion backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# CLI phase shorthand -> the phase values used by AACT's `studies.phase` and
# CT.gov API v2's `designModule.phases`.
PHASE_ALIASES = {
    "1": "PHASE1",
    "2": "PHASE2",
    "3": "PHASE3",
    "4": "PHASE4",
    "1/2": "PHASE1/PHASE2",
    "2/3": "PHASE2/PHASE3",
    "na": "NA",
}


def normalize_phases(phases: list[str]) -> list[str]:
    normalized = []
    for raw in phases:
        key = raw.strip().lower()
        if key not in PHASE_ALIASES:
            valid = ", ".join(sorted(PHASE_ALIASES))
            raise ValueError(f"Unrecognized phase {raw!r}. Valid values: {valid}")
        normalized.append(PHASE_ALIASES[key])
    return normalized


@dataclass(frozen=True)
class PullFilters:
    phases: tuple[str, ...]
    limit: int
    since: date | None = None
    ta: tuple[str, ...] | None = None
    # Case-insensitive substrings matched against the lead sponsor's name,
    # OR'd. Both backends apply this server-side.
    org: tuple[str, ...] | None = None
    # Drug-class ids. Neither backend can express this server-side, so it is
    # applied client-side before `limit` truncates, like `ta`.
    drug_class: tuple[str, ...] | None = None
    # Drop and recreate raw.* rather than upsert into it.
    replace: bool = False
    # Land the results section. Skipping saves warehouse size, not network:
    # the CT.gov API returns it in the same payload.
    with_results: bool = True

    def as_dict(self) -> dict:
        d = {
            "phases": list(self.phases),
            "limit": self.limit,
            "since": self.since.isoformat() if self.since else None,
        }
        if self.ta:
            d["ta"] = list(self.ta)
        if self.org:
            d["org"] = list(self.org)
        if self.drug_class:
            d["drug_class"] = list(self.drug_class)
        if self.replace:
            d["replace"] = True
        if not self.with_results:
            d["with_results"] = False
        return d
