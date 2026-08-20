"""CDISC CT codes, the code-system constants, and the outcome_type -> level map.

Every `Code` USDM emits needs `code`, `codeSystem`, `codeSystemVersion` and
`decode`, all four required. They are asserted here and nowhere else, because
`codeSystem`/`codeSystemVersion` are the two fields most likely to be silently
wrong in a document nobody validates by eye.

The two constants are taken from CDISC's own published example documents
(DDF-RA/Documents/Examples/*), where all three use exactly these values. Bump
`CODE_SYSTEM_VERSION` when the deployment targets a newer CT package.

Codelists: endpoint level is C188726, objective level C188725 (DDF-RA
Deliverables/CT/USDM_CT.xlsx at tag v4.0.0).
"""

from __future__ import annotations

import re

USDM_VERSION = "4.0.0"
SYSTEM_NAME = "clinical-study-endpoint-knowledge-base"

CODE_SYSTEM = "http://www.cdisc.org"
CODE_SYSTEM_VERSION = "2024-09-27"

#: Namespace for every ExtensionAttribute this projection emits. Bumped to v2
#: by docs/USDM_PROJECTION_INTEGRITY_SPEC.md's extension profile (change 4):
#: `decomposition` split into `decomposition` (semantics) + `conformance`
#: (how it was decided), `derived` became a per-attribute flag, and
#: `timepointRole` joined the decomposition. One flag day, with the event
#: spec, not a piecemeal bump.
EXTENSION_NS = "urn:x-endpoints-kb:usdm:ext:v2"

#: Prefix for the `reference` a BiomedicalConceptSurrogate carries back to the
#: vocabulary term it stands for. Resolvable when the API is served.
VOCAB_REFERENCE_BASE = "/v4/vocab"

#: docs/USDM_PROJECTION_INTEGRITY_SPEC.md change 5: a machine-readable
#: statement, first key in the module envelope, that its shape is the
#: knowledge-base module -- USDM class instances inside a KB envelope --
#: not USDM's own Wrapper. The wrapper envelope carries no `profile`; it is
#: the standard's own shape and needs no disclaimer.
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

#: The two ingestion backends do not agree on this vocabulary: `ctgov_api`
#: writes lowercase primary/secondary/other, while `aact` runs `SELECT
#: outcomes.*` and passes AACT's title-case values through untouched. Both are
#: mapped here, on a normalised key, and anything unrecognised raises rather
#: than defaulting to exploratory -- a silent default would mislabel a primary
#: endpoint, which is the one thing a consumer of this API most relies on.
_LEVEL_BY_OUTCOME_TYPE: dict[str, str] = {
    "primary": "primary",
    "secondary": "secondary",
    "other": "exploratory",
    "other pre specified": "exploratory",
    "other prespecified": "exploratory",
    "post hoc": "exploratory",
}


class UnknownOutcomeType(ValueError):
    """An outcome_type neither backend's vocabulary accounts for."""


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
