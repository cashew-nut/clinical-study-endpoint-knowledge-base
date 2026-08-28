"""Pull filters shared by every ingestion backend (AACT, ClinicalTrials.gov API)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# User-facing phase shorthand -> the phase enum values used by both AACT's
# `studies.phase` column and CT.gov API v2's `designModule.phases`.
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
    """Map CLI phase shorthand (e.g. "3", "1/2") onto the shared phase values."""
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
    #: Case-insensitive substring fragments matched against the *lead* sponsor's
    #: name only (never collaborators) -- OR'd together, same as `ta`. Unlike
    #: `ta`, both backends can apply this server-side (AREA[LeadSponsorName] /
    #: a `ctgov.sponsors` join), so it needs none of `ta`'s client-side scan-cap
    #: machinery.
    org: tuple[str, ...] | None = None
    #: `--replace`: drop and recreate raw.* rather than upsert into it, so this
    #: pull's results are all the warehouse holds afterward -- studies landed by
    #: any earlier pull, with any filters, are discarded. False (upsert) is the
    #: default everywhere else in this codebase assumes.
    replace: bool = False

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
        if self.replace:
            d["replace"] = True
        return d
