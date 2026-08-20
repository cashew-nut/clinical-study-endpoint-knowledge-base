"""Readable sequential ids and the `usdm:ref` element.

CDISC's published examples use `Endpoint_1`, `Objective_2`, `Code_622`,
`SyntaxTemplateDictionary_1` -- no published USDM document emits UUIDs -- so
this projection matches that. Sequential ids are only as stable as their
ordering, and the ordering is fixed and content-derived (see
docs/USDM_ENDPOINTS_API_SPEC.md, "Identity"): same warehouse state produces a
byte-identical document.

The durable identity is the conformed `endpoint_id` content hash, which travels
in the endpoint's extensions -- `Endpoint_7` is a position, not a name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr


@dataclass
class IdFactory:
    """Mints `ClassName_N`, N counting from 1 per class, in call order."""

    _counts: dict[str, int] = field(default_factory=dict)

    def mint(self, klass: str) -> str:
        n = self._counts.get(klass, 0) + 1
        self._counts[klass] = n
        return f"{klass}_{n}"

    def count(self, klass: str) -> int:
        return self._counts.get(klass, 0)


def usdm_ref(klass: str, instance_id: str, attribute: str) -> str:
    """The reference form every published USDM document uses.

    Verbatim from CDISC_Pilot_Study.json:
        <usdm:ref klass="Quantity" id="Quantity_9" attribute="value"></usdm:ref>
    """
    return (
        f"<usdm:ref klass={quoteattr(klass)} id={quoteattr(instance_id)} "
        f"attribute={quoteattr(attribute)}></usdm:ref>"
    )
