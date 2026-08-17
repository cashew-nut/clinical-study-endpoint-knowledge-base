"""Source scoping: Essie filter construction, presets, and the truncation guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest

from ceskb.cli import PRESETS, _apply_preset, build_parser
from ceskb.ingest.pipeline import ingest
from ceskb.ingest.sources import CtgovApiSource, FixtureSource, build_source
from ceskb.store.db import connect, get_watermark, initialise, load_vocabulary_into_db


# --------------------------------------------------------------------------- #
# Essie filter construction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, None),
        ({"phases": ("PHASE3",)}, "AREA[Phase]PHASE3"),
        (
            {"phases": ("PHASE2", "PHASE3")},
            "(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3)",
        ),
        ({"study_type": "INTERVENTIONAL"}, "AREA[StudyType]INTERVENTIONAL"),
        (
            {"updated_since": "2026-01-01"},
            "AREA[LastUpdatePostDate]RANGE[2026-01-01,MAX]",
        ),
        (
            {"phases": ("PHASE3",), "study_type": "INTERVENTIONAL", "updated_since": "2026-01-01"},
            "AREA[Phase]PHASE3 AND AREA[StudyType]INTERVENTIONAL "
            "AND AREA[LastUpdatePostDate]RANGE[2026-01-01,MAX]",
        ),
    ],
)
def test_advanced_filter_construction(kwargs, expected):
    assert CtgovApiSource(**kwargs)._advanced_filter() == expected


def test_page_size_never_exceeds_the_requested_cap():
    """Asking for 10 studies should not pull a 200-study page."""
    assert CtgovApiSource(max_studies=10)._params(None)["pageSize"] == 10
    assert CtgovApiSource(max_studies=10_000)._params(None)["pageSize"] == CtgovApiSource().config.page_size
    assert CtgovApiSource()._params(None)["pageSize"] == CtgovApiSource().config.page_size


def test_sort_is_passed_through_only_when_set():
    assert "sort" not in CtgovApiSource()._params(None)
    assert CtgovApiSource(sort="LastUpdatePostDate:desc")._params(None)["sort"] == "LastUpdatePostDate:desc"


def test_page_token_is_included_when_paginating():
    assert CtgovApiSource()._params("abc123")["pageToken"] == "abc123"


def test_build_source_rejects_an_unknown_kind():
    from ceskb.ingest.sources import SourceError

    with pytest.raises(SourceError):
        build_source("pubmed")


# --------------------------------------------------------------------------- #
# egress policy denial
# --------------------------------------------------------------------------- #
def test_proxy_policy_denial_is_recognised_and_not_retried():
    """A 403 to CONNECT is permanent; spending the backoff budget on it is waste."""
    import httpx

    from ceskb.ingest.sources import _is_egress_denial

    request = httpx.Request("GET", "https://clinicaltrials.gov/api/v2/studies")
    assert _is_egress_denial(httpx.ProxyError("403 Forbidden", request=request))
    assert _is_egress_denial(httpx.ConnectError("403 Forbidden", request=request))
    assert _is_egress_denial(httpx.ConnectError("407 Proxy Authentication Required", request=request))
    # Genuinely transient failures must stay retryable.
    assert not _is_egress_denial(httpx.ConnectTimeout("timed out", request=request))
    assert not _is_egress_denial(httpx.ReadError("connection reset", request=request))


def test_egress_denial_raises_immediately_with_actionable_guidance(monkeypatch):
    import httpx

    from ceskb.ingest.sources import SourceError

    calls = {"n": 0}

    def _denied(self, *args, **kwargs):
        calls["n"] += 1
        raise httpx.ProxyError(
            "403 Forbidden", request=httpx.Request("GET", "https://clinicaltrials.gov/")
        )

    monkeypatch.setattr(httpx.Client, "get", _denied)
    source = CtgovApiSource(max_studies=1)
    with pytest.raises(SourceError) as excinfo:
        list(source.iter_studies())

    assert calls["n"] == 1, "a policy denial must not be retried"
    message = str(excinfo.value)
    assert "network egress policy" in message
    assert "--source fixtures" in message


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
def test_preset_supplies_defaults():
    argv = ["refresh", "--preset", "phase3-recent-100"]
    args = build_parser().parse_args(argv)
    _apply_preset(args, argv)
    assert args.source == "ctgov"
    assert args.phase == ["PHASE3"]
    assert args.max_studies == 100
    assert args.sort == "LastUpdatePostDate:desc"


def test_explicit_flags_beat_the_preset():
    argv = ["refresh", "--preset", "phase3-recent-100", "--max-studies", "25"]
    args = build_parser().parse_args(argv)
    _apply_preset(args, argv)
    assert args.max_studies == 25
    assert args.phase == ["PHASE3"]  # untouched keys still come from the preset


def test_every_preset_is_well_formed():
    parser = build_parser()
    for name, preset in PRESETS.items():
        argv = ["refresh", "--preset", name]
        args = parser.parse_args(argv)
        _apply_preset(args, argv)
        assert args.source in {"ctgov", "fixtures"}, name
        assert preset.get("_note"), f"{name} should explain what it selects"


# --------------------------------------------------------------------------- #
# truncation guard
# --------------------------------------------------------------------------- #
@dataclass
class _CappedSource:
    """A source that stops early, mimicking a max_studies-capped API pull."""

    cap: int
    name: str = "capped"
    truncated: bool = field(default=False)

    def iter_studies(self) -> Iterator[dict[str, Any]]:
        for index, record in enumerate(FixtureSource().iter_studies()):
            if index >= self.cap:
                self.truncated = True
                return
            yield record

    def describe(self) -> dict[str, Any]:
        return {"source": self.name, "cap": self.cap}


def _ingest_with(tmp_path, vocab, source, filename):
    path = tmp_path / filename
    with connect(path) as conn:
        initialise(conn)
        load_vocabulary_into_db(conn, vocab)
        stats = ingest(conn, source, write_bronze=False)
        watermark = get_watermark(conn, source.name, "last_update_posted")
    return stats, watermark


def test_truncated_run_does_not_advance_the_watermark(tmp_path, vocab):
    """A capped run saw only the head of the result set.

    Advancing the watermark would make the next incremental run start after records
    this run never fetched, skipping them permanently.
    """
    stats, watermark = _ingest_with(tmp_path, vocab, _CappedSource(cap=3), "capped.duckdb")
    assert stats.truncated is True
    assert stats.studies_seen == 3
    assert stats.max_last_update_posted is not None
    assert watermark is None, "a truncated run must leave the watermark untouched"
    assert stats.as_dict()["watermark_advanced"] is False


def test_complete_run_does_advance_the_watermark(tmp_path, vocab):
    stats, watermark = _ingest_with(tmp_path, vocab, FixtureSource(), "full.duckdb")
    assert stats.truncated is False
    assert watermark == stats.max_last_update_posted
    assert stats.as_dict()["watermark_advanced"] is True
