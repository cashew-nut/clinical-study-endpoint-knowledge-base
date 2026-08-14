"""Study sources.

A source yields raw study records in ClinicalTrials.gov API v2 shape. Keeping the
shape fixed and the transport pluggable means the normalisation, classification and
projection stages never learn where a record came from, and a second registry can be
added later by writing an adapter that emits the same shape.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

import httpx

from ceskb.config import CTGOV, PATHS, CtgovConfig


class SourceError(Exception):
    """Raised when a source cannot supply records."""


EGRESS_DENIED_MESSAGE = (
    "ClinicalTrials.gov is blocked by this environment's network egress policy "
    "({error}).\n"
    "\n"
    "This is a policy denial at the proxy in front of the container, not a transient\n"
    "network fault, so retrying will not help. Allowlist clinicaltrials.gov for the\n"
    "environment, then re-run. Until then, use --source fixtures to exercise the\n"
    "pipeline against the synthetic corpus."
)

#: A proxy answering 403 or 407 to CONNECT surfaces as a transport error rather than
#: an HTTP status, because the tunnel was refused before any request was sent. Without
#: this check the retry loop treats an organisation policy denial as a flaky network
#: and spends its whole backoff budget on it.
_EGRESS_DENIAL_MARKERS = ("403 forbidden", "407 proxy", "proxy authentication")


def _is_egress_denial(exc: Exception) -> bool:
    if isinstance(exc, httpx.ProxyError):
        return True
    if isinstance(exc, httpx.TransportError):
        text = str(exc).lower()
        return any(marker in text for marker in _EGRESS_DENIAL_MARKERS)
    return False


class StudySource(Protocol):
    """Yields raw study records and identifies itself for provenance."""

    name: str

    def iter_studies(self) -> Iterator[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def describe(self) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------- #
# ClinicalTrials.gov API v2
# --------------------------------------------------------------------------- #
class _RateLimiter:
    """Minimum-interval limiter. Simple, and the API's limit is per-IP anyway."""

    def __init__(self, requests_per_minute: int) -> None:
        self._min_interval = 60.0 / max(requests_per_minute, 1)
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


@dataclass
class CtgovApiSource:
    """Paginates ClinicalTrials.gov API v2 `/studies`.

    Incremental refresh uses the Essie advanced filter on LastUpdatePostDate, so a
    scheduled run transfers only what changed rather than re-walking the registry.
    """

    query_term: str | None = None
    condition: str | None = None
    updated_since: str | None = None
    max_studies: int | None = None
    phases: tuple[str, ...] = ()
    study_type: str | None = None
    sort: str | None = None
    config: CtgovConfig = CTGOV
    name: str = "clinicaltrials.gov"

    #: Set when iteration stopped because max_studies was reached rather than because
    #: the source ran out. The pipeline reads this: a truncated run has not seen
    #: everything, so advancing the update watermark would permanently skip whatever
    #: was left unfetched.
    truncated: bool = False

    def _advanced_filter(self) -> str | None:
        """Build an Essie expression from the active filters.

        Essie is used rather than the dedicated filter parameters because it composes:
        phase, study type and date range join with AND in a single expression.
        """
        clauses: list[str] = []
        if self.phases:
            joined = " OR ".join(f"AREA[Phase]{p}" for p in self.phases)
            clauses.append(f"({joined})" if len(self.phases) > 1 else joined)
        if self.study_type:
            clauses.append(f"AREA[StudyType]{self.study_type}")
        if self.updated_since:
            clauses.append(f"AREA[LastUpdatePostDate]RANGE[{self.updated_since},MAX]")
        return " AND ".join(clauses) if clauses else None

    def _params(self, page_token: str | None) -> dict[str, Any]:
        page_size = self.config.page_size
        if self.max_studies is not None:
            page_size = min(page_size, max(self.max_studies, 1))
        params: dict[str, Any] = {
            "format": "json",
            "pageSize": page_size,
            "countTotal": "true",
            "fields": "|".join(self.config.fields),
        }
        if self.query_term:
            params["query.term"] = self.query_term
        if self.condition:
            params["query.cond"] = self.condition
        advanced = self._advanced_filter()
        if advanced:
            params["filter.advanced"] = advanced
        if self.sort:
            params["sort"] = self.sort
        if page_token:
            params["pageToken"] = page_token
        return params

    def _get(self, client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                response = client.get("/studies", params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if _is_egress_denial(exc):
                    raise SourceError(EGRESS_DENIED_MESSAGE.format(error=exc)) from exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                # 4xx other than 429 will not succeed on retry.
                if status is not None and 400 <= status < 500 and status != 429:
                    raise SourceError(f"ClinicalTrials.gov rejected the request: {exc}") from exc
                if attempt == self.config.max_retries - 1:
                    break
                time.sleep(self.config.backoff_base_seconds * (2**attempt))
        raise SourceError(
            f"ClinicalTrials.gov unreachable after {self.config.max_retries} attempts: {last_error}"
        ) from last_error

    def iter_studies(self) -> Iterator[dict[str, Any]]:
        limiter = _RateLimiter(self.config.requests_per_minute)
        yielded = 0
        page_token: str | None = None
        self.truncated = False
        with httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            headers={"User-Agent": self.config.user_agent, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            while True:
                limiter.wait()
                payload = self._get(client, self._params(page_token))
                studies = payload.get("studies") or []
                if not studies:
                    return
                for study in studies:
                    yield study
                    yielded += 1
                    if self.max_studies is not None and yielded >= self.max_studies:
                        self.truncated = True
                        return
                page_token = payload.get("nextPageToken")
                if not page_token:
                    return

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "base_url": self.config.base_url,
            "query_term": self.query_term,
            "condition": self.condition,
            "updated_since": self.updated_since,
            "max_studies": self.max_studies,
            "phases": list(self.phases),
            "study_type": self.study_type,
            "sort": self.sort,
            "filter.advanced": self._advanced_filter(),
        }


# --------------------------------------------------------------------------- #
# Local fixtures
# --------------------------------------------------------------------------- #
@dataclass
class FixtureSource:
    """Reads study records from local JSON files.

    Used by the test suite and by any environment whose egress policy blocks the
    registry. Fixture records are synthetic and carry a `_synthetic` marker that the
    normaliser copies onto the study row, so demonstration data can never be mistaken
    for registry data in the database or the UI.
    """

    directory: Path | None = None
    name: str = "fixtures"

    def _files(self) -> list[Path]:
        directory = self.directory or PATHS.fixtures
        if not directory.exists():
            raise SourceError(f"fixture directory not found: {directory}")
        return sorted(directory.glob("*.json"))

    def iter_studies(self) -> Iterator[dict[str, Any]]:
        for path in self._files():
            payload = json.loads(path.read_text())
            studies = payload.get("studies") if isinstance(payload, dict) else payload
            if studies is None:
                studies = [payload]
            for study in studies:
                study.setdefault("_synthetic", True)
                yield study

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "directory": str(self.directory or PATHS.fixtures),
            "files": [p.name for p in self._files()],
            "synthetic": True,
        }


def build_source(kind: str, **kwargs: Any) -> StudySource:
    if kind == "ctgov":
        return CtgovApiSource(**kwargs)
    if kind == "fixtures":
        return FixtureSource(**kwargs)
    raise SourceError(f"unknown source '{kind}' (expected 'ctgov' or 'fixtures')")
