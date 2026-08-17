"""Score the classifier against a gold set.

The headline number is per-concept **precision**, not coverage and not recall, because
the two kinds of error are not symmetric here. An outcome that matches nothing is
visibly absent: it sits in the gaps queue with its text, waiting for a rule. An outcome
that matches the *wrong* concept is invisible: it silently joins a prevalence count, a
cross-study comparison, a USDM document. Recall gaps announce themselves; precision
failures do not. So the CI gate is set on precision and recall is allowed to lag.

Scoring deliberately refuses to grade some items:

* **stale** -- the outcome text changed since annotation, so the annotator's judgement
  is about words that no longer exist;
* **absent** -- the outcome is not in this database, usually a different registry scope.

Both are reported rather than dropped. A score computed over a shrinking denominator
while the excluded pile grows is exactly the sort of number that quietly stops meaning
anything, so the excluded counts sit next to the score wherever it is shown.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import duckdb

from ceskb.config import DERIVATION_VERSION
from ceskb.evaluate.gold import NO_MATCH, GoldSet
from ceskb.store.db import _json, utcnow

#: Printed whenever a self-annotated set is scored. The number such a set produces is
#: self-consistency, not accuracy: the same party wrote the rules and the answers, and
#: shares any misconception between them. It is still useful -- it catches regressions
#: and pins behaviour -- but it must never be quoted as a quality measure.
SELF_ANNOTATION_CAVEAT = (
    "This gold set is self-annotated: the same party authored the rules and the "
    "answers, so any shared misconception is invisible to it. Treat these figures as "
    "regression detection, not as accuracy. A second independent annotator is what "
    "turns them into evidence."
)


@dataclass
class Prediction:
    outcome_uid: str
    concept_id: str | None
    axes: dict[str, str]
    present: bool
    record_hash: str | None


def _load_predictions(conn: duckdb.DuckDBPyConnection, uids: list[str]) -> dict[str, Prediction]:
    if not uids:
        return {}
    placeholders = ",".join("?" for _ in uids)

    outcomes = {
        row[0]: row[1]
        for row in conn.execute(
            f"SELECT outcome_uid, record_hash FROM study_outcome WHERE outcome_uid IN ({placeholders})",
            uids,
        ).fetchall()
    }
    specs = {
        row[0]: row[1]
        for row in conn.execute(
            f"SELECT outcome_uid, concept_id FROM endpoint_spec WHERE outcome_uid IN ({placeholders})",
            uids,
        ).fetchall()
    }
    axes: dict[str, dict[str, str]] = defaultdict(dict)
    for outcome_uid, axis_id, term_id in conn.execute(
        f"""
        SELECT e.outcome_uid, a.axis_id, a.term_id
        FROM endpoint_spec e JOIN endpoint_spec_axis a USING (spec_id)
        WHERE e.outcome_uid IN ({placeholders})
        """,
        uids,
    ).fetchall():
        axes[outcome_uid][axis_id] = term_id

    return {
        uid: Prediction(
            outcome_uid=uid,
            concept_id=specs.get(uid),
            axes=dict(axes.get(uid, {})),
            present=uid in outcomes,
            record_hash=outcomes.get(uid),
        )
        for uid in uids
    }


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | None]:
    """Precision, recall and F1, with the undefined cases reported as None.

    Precision is undefined when the classifier never predicted the label at all: there
    are no predictions to be right or wrong about. Reporting that as 0.0 would make a
    precision gate fail on an absence of evidence -- the label would be indicted for
    never having been guessed -- so it is None here and skipped by the gate. Recall is
    what carries the signal in that case, and it correctly reads 0.
    """
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
    }


def score(conn: duckdb.DuckDBPyConnection, gold: GoldSet) -> dict[str, Any]:
    """Score current classifications against one gold set."""
    items = list(gold.items)
    predictions = _load_predictions(conn, [i.outcome_uid for i in items])

    scored: list[tuple[str, str, str]] = []  # (uid, gold_label, predicted_label)
    excluded: list[dict[str, str]] = []

    for item in items:
        prediction = predictions.get(item.outcome_uid)
        if prediction is None or not prediction.present:
            excluded.append({"outcome_uid": item.outcome_uid, "why": "absent_from_database"})
            continue
        if item.source_hash and prediction.record_hash != item.source_hash:
            excluded.append({"outcome_uid": item.outcome_uid, "why": "source_text_changed"})
            continue
        scored.append(
            (
                item.outcome_uid,
                item.concept_id or NO_MATCH,
                prediction.concept_id or NO_MATCH,
            )
        )

    correct = sum(1 for _, g, p in scored if g == p)
    accuracy = correct / len(scored) if scored else 0.0

    # Per-concept counts. NO_MATCH participates: predicting "nothing fits" correctly is
    # a success, and predicting it wrongly is a recall failure that must be visible.
    labels = sorted({g for _, g, _ in scored} | {p for _, _, p in scored})
    per_concept: dict[str, dict[str, Any]] = {}
    for label in labels:
        tp = sum(1 for _, g, p in scored if g == label and p == label)
        fp = sum(1 for _, g, p in scored if g != label and p == label)
        fn = sum(1 for _, g, p in scored if g == label and p != label)
        per_concept[label] = {"support": tp + fn, "tp": tp, "fp": fp, "fn": fn, **_prf(tp, fp, fn)}

    # Macro averages over concepts the gold set actually attests, so a concept the
    # classifier invented once cannot drag the mean around. Undefined components are
    # left out of their own average rather than counted as zero.
    attested = [c for c, m in per_concept.items() if m["support"] > 0]
    macro: dict[str, float] = {}
    for key in ("precision", "recall", "f1"):
        values = [
            per_concept[c][key] for c in attested if per_concept[c][key] is not None
        ]
        macro[key] = round(sum(values) / len(values), 4) if values else 0.0

    confusion = Counter((g, p) for _, g, p in scored if g != p)

    by_difficulty: dict[str, dict[str, Any]] = {}
    difficulty_of = {i.outcome_uid: i.difficulty for i in items}
    for level in ("clear", "judgement", "ambiguous"):
        subset = [(u, g, p) for u, g, p in scored if difficulty_of.get(u) == level]
        if subset:
            hits = sum(1 for _, g, p in subset if g == p)
            by_difficulty[level] = {
                "items": len(subset),
                "correct": hits,
                "accuracy": round(hits / len(subset), 4),
            }

    # Axis accuracy, only over axes the annotator actually judged.
    axis_totals: Counter[str] = Counter()
    axis_hits: Counter[str] = Counter()
    axis_errors: list[dict[str, str]] = []
    scored_uids = {u for u, _, _ in scored}
    for item in items:
        if item.outcome_uid not in scored_uids or not item.axes:
            continue
        predicted_axes = predictions[item.outcome_uid].axes
        for axis_id, expected in item.axes.items():
            axis_totals[axis_id] += 1
            actual = predicted_axes.get(axis_id)
            if actual == expected:
                axis_hits[axis_id] += 1
            else:
                axis_errors.append(
                    {
                        "outcome_uid": item.outcome_uid,
                        "axis_id": axis_id,
                        "expected": expected,
                        "actual": actual or "<missing>",
                    }
                )

    report: dict[str, Any] = {
        "gold_set_id": gold.gold_set_id,
        "gold_set_version": gold.version,
        "annotator": gold.annotator,
        "independence": gold.independence,
        "derivation_version": DERIVATION_VERSION,
        "items_total": len(items),
        "items_scored": len(scored),
        "items_excluded": len(excluded),
        "excluded": excluded,
        "concept_accuracy": round(accuracy, 4),
        "concepts_correct": correct,
        "macro_precision": macro["precision"],
        "macro_recall": macro["recall"],
        "macro_f1": macro["f1"],
        "per_concept": per_concept,
        "by_difficulty": by_difficulty,
        "confusion": [
            {"gold": g, "predicted": p, "count": n} for (g, p), n in confusion.most_common()
        ],
        "errors": [
            {
                "outcome_uid": u,
                "gold": g,
                "predicted": p,
                "text": next((i.text for i in items if i.outcome_uid == u), ""),
            }
            for u, g, p in scored
            if g != p
        ],
        "axis_accuracy": {
            axis_id: {
                "judged": axis_totals[axis_id],
                "correct": axis_hits[axis_id],
                "accuracy": round(axis_hits[axis_id] / axis_totals[axis_id], 4),
            }
            for axis_id in sorted(axis_totals)
        },
        "axis_errors": axis_errors,
    }
    if not gold.is_independent:
        report["caveat"] = SELF_ANNOTATION_CAVEAT
    return report


def persist(conn: duckdb.DuckDBPyConnection, report: dict[str, Any]) -> str:
    """Store a scored run so accuracy is a tracked series, not a one-off claim."""
    evaluation_id = uuid.uuid4().hex[:16]
    conn.execute(
        """
        INSERT INTO evaluation_run (
            evaluation_id, gold_set_id, gold_set_version, independence,
            derivation_version, evaluated_at, items_total, items_scored, items_stale,
            concept_accuracy, macro_precision, macro_recall, macro_f1, report
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            evaluation_id,
            report["gold_set_id"],
            report.get("gold_set_version"),
            report["independence"],
            report["derivation_version"],
            utcnow(),
            report["items_total"],
            report["items_scored"],
            report["items_excluded"],
            report["concept_accuracy"],
            report["macro_precision"],
            report["macro_recall"],
            report["macro_f1"],
            _json(report),
        ],
    )
    return evaluation_id


def check_thresholds(
    report: dict[str, Any],
    min_accuracy: float | None = None,
    min_precision: float | None = None,
    min_recall: float | None = None,
    max_excluded: int | None = None,
) -> list[str]:
    """Return gate failures. Empty means the run passes.

    ``min_precision`` is checked per concept rather than on the macro average on
    purpose: one concept mapping badly is a real defect, and averaging hides it behind
    forty that map well.
    """
    failures: list[str] = []
    if min_accuracy is not None and report["concept_accuracy"] < min_accuracy:
        failures.append(
            f"concept accuracy {report['concept_accuracy']:.3f} < required {min_accuracy:.3f}"
        )
    if min_recall is not None and report["macro_recall"] < min_recall:
        failures.append(
            f"macro recall {report['macro_recall']:.3f} < required {min_recall:.3f}"
        )
    if min_precision is not None:
        for concept_id, metrics in sorted(report["per_concept"].items()):
            # No support means the gold set does not attest the label; no precision
            # means it was never predicted. Neither is a precision failure.
            if metrics["support"] == 0 or metrics["precision"] is None:
                continue
            if metrics["precision"] < min_precision:
                failures.append(
                    f"{concept_id}: precision {metrics['precision']:.3f} < required "
                    f"{min_precision:.3f} ({metrics['fp']} false positive(s))"
                )
    if max_excluded is not None and report["items_excluded"] > max_excluded:
        failures.append(
            f"{report['items_excluded']} gold items excluded from scoring "
            f"(limit {max_excluded}) -- re-annotate before trusting the score"
        )
    return failures
