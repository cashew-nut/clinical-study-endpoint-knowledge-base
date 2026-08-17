"""Gold-standard annotation sets, and agreement between annotators.

An annotation is a claim about a specific piece of text. Two things follow, and both
are enforced here rather than left to discipline.

**An annotation is bound to the text it was made against.** Each item may carry the
outcome's ``record_hash``. When the registry rewrites an outcome the hash moves and the
item is *stale*: it is excluded from scoring rather than being silently graded against
words the annotator never read. A gold set that quietly re-points at new text is worse
than no gold set, because it produces a number that looks like evidence.

**Agreement comes before accuracy.** If two annotators applying the same guideline
disagree about what an outcome is, the disagreement is not the classifier's fault and
cannot be fixed by tuning it -- the vocabulary or the guideline is underspecified. So
``agreement()`` exists to be run first, and the honest order of work is: measure
agreement, fix the definitions, then measure accuracy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator

from ceskb.config import PATHS


class GoldError(Exception):
    """Raised when a gold set is malformed or refers to unknown vocabulary."""


@dataclass(frozen=True)
class GoldItem:
    outcome_uid: str
    concept_id: str | None
    text: str = ""
    source_hash: str | None = None
    axes: dict[str, str] = field(default_factory=dict)
    difficulty: str = "clear"
    note: str | None = None

    @property
    def expects_no_match(self) -> bool:
        return self.concept_id is None


@dataclass(frozen=True)
class GoldSet:
    gold_set_id: str
    annotator: str
    independence: str
    items: tuple[GoldItem, ...]
    version: str | None = None
    description: str | None = None
    annotated_at: str | None = None
    guideline: str | None = None
    path: Path | None = None

    @property
    def is_independent(self) -> bool:
        return self.independence in {"independent", "adjudicated"}

    def by_uid(self) -> dict[str, GoldItem]:
        return {item.outcome_uid: item for item in self.items}


def _validator() -> Draft202012Validator:
    schema = json.loads((PATHS.schemas / "goldset.schema.json").read_text())
    return Draft202012Validator(schema)


def load_gold_set(path: Path | str) -> GoldSet:
    target = Path(path)
    if not target.exists():
        raise GoldError(f"gold set not found: {target}")

    payload = yaml.safe_load(target.read_text())
    errors = sorted(_validator().iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        detail = "\n".join(
            f"  at {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:20]
        )
        raise GoldError(f"{target} failed goldset.schema.json:\n{detail}")

    seen: set[str] = set()
    items: list[GoldItem] = []
    for record in payload["items"]:
        uid = record["outcome_uid"]
        if uid in seen:
            raise GoldError(f"{target}: {uid} annotated twice")
        seen.add(uid)
        items.append(
            GoldItem(
                outcome_uid=uid,
                concept_id=record["concept_id"],
                text=record.get("text", ""),
                source_hash=record.get("source_hash"),
                axes=dict(record.get("axes") or {}),
                difficulty=record.get("difficulty", "clear"),
                note=record.get("note"),
            )
        )

    return GoldSet(
        gold_set_id=payload["gold_set_id"],
        annotator=payload["annotator"],
        independence=payload["independence"],
        items=tuple(items),
        version=payload.get("version"),
        description=payload.get("description"),
        annotated_at=payload.get("annotated_at"),
        guideline=payload.get("guideline"),
        path=target,
    )


def load_gold_sets(directory: Path | None = None) -> list[GoldSet]:
    target = directory or PATHS.gold
    if not target.exists():
        return []
    return [load_gold_set(p) for p in sorted(target.glob("*.yaml"))]


def check_gold_set(gold: GoldSet, vocab: Any) -> list[str]:
    """Cross-check annotations against the vocabulary, so typos fail in CI."""
    problems: list[str] = []
    for item in gold.items:
        if item.concept_id is not None and item.concept_id not in vocab.concepts:
            problems.append(f"{item.outcome_uid}: unknown concept '{item.concept_id}'")
        for axis_id, term_id in item.axes.items():
            axis = vocab.axes.get(axis_id)
            if axis is None:
                problems.append(f"{item.outcome_uid}: unknown axis '{axis_id}'")
            elif term_id not in axis.terms:
                problems.append(
                    f"{item.outcome_uid}: '{term_id}' is not a term of axis '{axis_id}'"
                )
    return problems


# --------------------------------------------------------------------------- #
# inter-annotator agreement
# --------------------------------------------------------------------------- #
def cohens_kappa(pairs: Iterable[tuple[str, str]]) -> float | None:
    """Chance-corrected agreement over paired categorical labels.

    Raw percent agreement flatters a skewed label set: if 80% of outcomes are one
    concept, two annotators who both guess it blindly agree 80% of the time. Kappa
    subtracts the agreement expected from the marginal distributions alone, so it
    reports how much the annotators agree *beyond* what the skew already explains.

    Returns None when kappa is undefined -- one category everywhere, so chance
    agreement is already total and there is no headroom to measure.
    """
    observations = list(pairs)
    if not observations:
        return None

    total = len(observations)
    agreed = sum(1 for a, b in observations if a == b)
    p_observed = agreed / total

    labels = {label for pair in observations for label in pair}
    p_expected = 0.0
    for label in labels:
        p_a = sum(1 for a, _ in observations if a == label) / total
        p_b = sum(1 for _, b in observations if b == label) / total
        p_expected += p_a * p_b

    if p_expected >= 1.0:
        return None
    return (p_observed - p_expected) / (1.0 - p_expected)


def interpret_kappa(kappa: float | None) -> str:
    """Landis and Koch's bands, named so a number does not have to be argued about."""
    if kappa is None:
        return "undefined"
    if kappa < 0.0:
        return "worse than chance"
    if kappa < 0.21:
        return "slight"
    if kappa < 0.41:
        return "fair"
    if kappa < 0.61:
        return "moderate"
    if kappa < 0.81:
        return "substantial"
    return "almost perfect"


#: Labels a null annotation, so "both said nothing fits" counts as agreement rather
#: than being dropped -- deciding an outcome is unclassifiable is a real judgement.
NO_MATCH = "__none__"


def agreement(first: GoldSet, second: GoldSet) -> dict[str, Any]:
    """Compare two independent annotations of the same outcomes.

    Run this before trusting any accuracy figure. Low agreement means the annotation
    task itself is ill-posed, and no amount of classifier work will fix it.
    """
    left, right = first.by_uid(), second.by_uid()
    shared = sorted(set(left) & set(right))

    pairs = [
        (left[uid].concept_id or NO_MATCH, right[uid].concept_id or NO_MATCH) for uid in shared
    ]
    kappa = cohens_kappa(pairs)
    disagreements = [
        {
            "outcome_uid": uid,
            "text": left[uid].text or right[uid].text,
            first.annotator: left[uid].concept_id,
            second.annotator: right[uid].concept_id,
        }
        for uid in shared
        if (left[uid].concept_id or NO_MATCH) != (right[uid].concept_id or NO_MATCH)
    ]

    return {
        "annotators": [first.annotator, second.annotator],
        "gold_sets": [first.gold_set_id, second.gold_set_id],
        "items_shared": len(shared),
        "only_in_first": sorted(set(left) - set(right)),
        "only_in_second": sorted(set(right) - set(left)),
        "percent_agreement": round(100.0 * sum(1 for a, b in pairs if a == b) / len(pairs), 1)
        if pairs
        else None,
        "cohens_kappa": round(kappa, 3) if kappa is not None else None,
        "interpretation": interpret_kappa(kappa),
        "disagreements": disagreements,
    }
