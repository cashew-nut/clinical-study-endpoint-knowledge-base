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

    def as_dict(self) -> dict:
        return {
            "phases": list(self.phases),
            "limit": self.limit,
            "since": self.since.isoformat() if self.since else None,
        }
