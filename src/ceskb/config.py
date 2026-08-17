"""Filesystem layout and tunables. One place so nothing hard-codes a path."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _repo_root() -> Path:
    env = os.environ.get("CESKB_ROOT")
    if env:
        return Path(env).resolve()
    # src/ceskb/config.py -> src/ceskb -> src -> repo root
    return Path(__file__).resolve().parents[2]


ROOT = _repo_root()


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    vocabularies: Path = ROOT / "vocabularies"
    axes: Path = ROOT / "vocabularies" / "axes"
    concepts: Path = ROOT / "vocabularies" / "concepts"
    rules: Path = ROOT / "rules"
    schemas: Path = ROOT / "schemas"
    review: Path = ROOT / "review"
    overrides: Path = ROOT / "review" / "overrides.yaml"
    gold: Path = ROOT / "review" / "gold"
    data: Path = ROOT / "data"
    bronze: Path = ROOT / "data" / "bronze"
    fixtures: Path = ROOT / "data" / "fixtures"
    exports: Path = ROOT / "data" / "exports"
    database: Path = ROOT / "data" / "ceskb.duckdb"
    web: Path = ROOT / "web"


PATHS = Paths()


@dataclass(frozen=True)
class CtgovConfig:
    """ClinicalTrials.gov API v2 access.

    The published guidance is roughly 50 requests per minute per IP, so the default
    rate limit sits below that with room for retries.
    """

    base_url: str = os.environ.get("CESKB_CTGOV_BASE_URL", "https://clinicaltrials.gov/api/v2")
    page_size: int = 200
    requests_per_minute: int = 40
    timeout_seconds: float = 60.0
    max_retries: int = 5
    backoff_base_seconds: float = 2.0
    user_agent: str = "ceskb/0.1 (clinical study endpoint knowledge base; +https://github.com/cashew-nut/clinical-study-endpoint-knowledge-base)"

    #: Modules pulled from each study record. Kept explicit so payloads stay small and
    #: so a schema change upstream surfaces as a missing field rather than silent drift.
    fields: tuple[str, ...] = field(
        default_factory=lambda: (
            "protocolSection.identificationModule",
            "protocolSection.statusModule",
            "protocolSection.sponsorCollaboratorsModule",
            "protocolSection.conditionsModule",
            "protocolSection.designModule",
            "protocolSection.armsInterventionsModule",
            "protocolSection.outcomesModule",
            "derivedSection.conditionBrowseModule",
            "hasResults",
        )
    )


CTGOV = CtgovConfig()

#: Bump when a change to rules, extractors, or vocabularies should invalidate derived
#: rows. Stored on every classification so a row can always be traced to the logic
#: that produced it.
DERIVATION_VERSION = "2026.08.17.1"
