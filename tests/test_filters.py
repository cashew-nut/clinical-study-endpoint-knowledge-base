from __future__ import annotations

import pytest

from clinical_endpoints.ingest.filters import PHASE_ALIASES, PullFilters, normalize_phases


def test_normalize_phases_maps_shorthand_to_shared_phase_values():
    assert normalize_phases(["3"]) == ["PHASE3"]
    assert normalize_phases(["1/2", "3"]) == ["PHASE1/PHASE2", "PHASE3"]


def test_normalize_phases_rejects_unknown_value():
    with pytest.raises(ValueError, match="Unrecognized phase"):
        normalize_phases(["5"])


def test_normalize_phases_covers_all_declared_aliases():
    # every alias should round-trip without raising
    assert normalize_phases(list(PHASE_ALIASES)) == list(PHASE_ALIASES.values())


def test_as_dict_omits_org_and_replace_by_default():
    d = PullFilters(phases=("PHASE3",), limit=500).as_dict()
    assert d == {"phases": ["PHASE3"], "limit": 500, "since": None}
    assert "org" not in d
    assert "replace" not in d


def test_as_dict_includes_org_when_set():
    d = PullFilters(phases=("PHASE3",), limit=500, org=("Pfizer", "AbbVie")).as_dict()
    assert d["org"] == ["Pfizer", "AbbVie"]


def test_as_dict_includes_replace_only_when_true():
    assert "replace" not in PullFilters(phases=("PHASE3",), limit=500, replace=False).as_dict()
    d = PullFilters(phases=("PHASE3",), limit=500, replace=True).as_dict()
    assert d["replace"] is True
