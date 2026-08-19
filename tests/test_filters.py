from __future__ import annotations

import pytest

from clinical_endpoints.ingest.filters import PHASE_ALIASES, normalize_phases


def test_normalize_phases_maps_shorthand_to_shared_phase_values():
    assert normalize_phases(["3"]) == ["PHASE3"]
    assert normalize_phases(["1/2", "3"]) == ["PHASE1/PHASE2", "PHASE3"]


def test_normalize_phases_rejects_unknown_value():
    with pytest.raises(ValueError, match="Unrecognized phase"):
        normalize_phases(["5"])


def test_normalize_phases_covers_all_declared_aliases():
    # every alias should round-trip without raising
    assert normalize_phases(list(PHASE_ALIASES)) == list(PHASE_ALIASES.values())
