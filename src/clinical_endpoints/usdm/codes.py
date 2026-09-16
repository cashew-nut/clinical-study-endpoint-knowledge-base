"""CDISC CT codes, code-system constants, and the outcome_type -> level map.

`CODE_SYSTEM` and `CODE_SYSTEM_VERSION` are taken from CDISC's published
example documents (DDF-RA/Documents/Examples/*). Codelists: endpoint level
C188726, objective level C188725 (USDM_CT.xlsx at tag v4.0.0).
"""

from __future__ import annotations

import re

USDM_VERSION = "4.0.0"
SYSTEM_NAME = "clinical-study-endpoint-knowledge-base"

CODE_SYSTEM = "http://www.cdisc.org"
CODE_SYSTEM_VERSION = "2024-09-27"

# Namespace for every ExtensionAttribute this projection emits.
EXTENSION_NS = "urn:x-endpoints-kb:usdm:ext:v2"

# Prefix for a BiomedicalConceptSurrogate's `reference` back to its vocabulary term.
VOCAB_REFERENCE_BASE = "/v4/vocab"

# First key of the module envelope, stating that its shape is the
# knowledge-base module and not USDM's own Wrapper.
MODULE_PROFILE = "urn:x-endpoints-kb:usdm:module:v2"

LEVEL_ORDER: tuple[str, ...] = ("primary", "secondary", "exploratory")

ENDPOINT_LEVEL_CODES: dict[str, tuple[str, str]] = {
    "primary": ("C94496", "Primary Endpoint"),
    "secondary": ("C139173", "Secondary Endpoint"),
    "exploratory": ("C170559", "Exploratory Endpoint"),
}

OBJECTIVE_LEVEL_CODES: dict[str, tuple[str, str]] = {
    "primary": ("C85826", "Primary Objective"),
    "secondary": ("C85827", "Secondary Objective"),
    "exploratory": ("C163559", "Exploratory Objective"),
}

# `ctgov_api` writes lowercase primary/secondary/other; `aact` passes AACT's
# title-case values through. Anything unrecognised raises rather than
# defaulting to exploratory.
_LEVEL_BY_OUTCOME_TYPE: dict[str, str] = {
    "primary": "primary",
    "secondary": "secondary",
    "other": "exploratory",
    "other pre specified": "exploratory",
    "other prespecified": "exploratory",
    "post hoc": "exploratory",
}


class UnknownOutcomeType(ValueError):
    pass


def normalise_outcome_type(outcome_type: str | None) -> str:
    return re.sub(r"[\s_\-]+", " ", (outcome_type or "").strip().lower())


def level_for_outcome_type(outcome_type: str | None) -> str:
    key = normalise_outcome_type(outcome_type)
    try:
        return _LEVEL_BY_OUTCOME_TYPE[key]
    except KeyError:
        raise UnknownOutcomeType(
            f"outcome_type {outcome_type!r} maps to no USDM endpoint level. "
            f"Known: {sorted(_LEVEL_BY_OUTCOME_TYPE)}"
        ) from None


def level_rank(level: str) -> int:
    return LEVEL_ORDER.index(level)
