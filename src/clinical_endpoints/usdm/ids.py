"""Sequential ids and the `usdm:ref` element.

CDISC's published examples use `Endpoint_1`, `Code_622`; no published USDM
document emits UUIDs. The ordering is content-derived, so the same warehouse
state produces the same document. The durable identity is the conformed
`endpoint_id` hash carried in the endpoint's extensions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr


@dataclass
class IdFactory:
    """Mints `ClassName_N`, N counting from 1 per class."""

    _counts: dict[str, int] = field(default_factory=dict)

    def mint(self, klass: str) -> str:
        n = self._counts.get(klass, 0) + 1
        self._counts[klass] = n
        return f"{klass}_{n}"

    def count(self, klass: str) -> int:
        return self._counts.get(klass, 0)


def usdm_ref(klass: str, instance_id: str, attribute: str) -> str:
    """As in CDISC_Pilot_Study.json:
    <usdm:ref klass="Quantity" id="Quantity_9" attribute="value"></usdm:ref>"""
    return (
        f"<usdm:ref klass={quoteattr(klass)} id={quoteattr(instance_id)} "
        f"attribute={quoteattr(attribute)}></usdm:ref>"
    )
