"""Reviewer decisions about individual outcomes.

Two properties drive the whole design.

**A decision must outlive the logic it corrects.** Overrides are keyed by
``outcome_uid``, never ``spec_id``. A ``spec_id`` is derived from
``DERIVATION_VERSION``, so keying on it would throw away every human judgement the
moment a rule changed -- precisely when those judgements are most valuable. Keying on
the outcome means a correction keeps applying across rule edits, vocabulary edits and
full rebuilds.

**A decision must not outlive its subject.** Each override records the outcome's
``record_hash`` at the time it was made. When the registry rewrites the outcome text
the hash moves, and the override goes *stale*: it stops being applied and surfaces for
re-review, because it was a judgement about words that no longer exist. Silently
carrying it over would be worse than having no override at all -- it would be an
unreviewed assertion wearing a reviewer's name.

The YAML file is the system of record and lives in git next to the vocabularies; the
database table is loaded from it. That is the same relationship Layer A has with
``vocabularies/``, and it means review decisions are diffable, reviewable in a pull
request, and never trapped inside a derived artefact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import yaml
from jsonschema import Draft202012Validator

from ceskb.config import PATHS
from ceskb.store.db import _json


class OverrideError(Exception):
    """Raised when an override file is malformed or refers to something unknown."""


#: A reviewer's decision about the concept has three states, not two: leave it alone,
#: it is X, or nothing fits. A plain nullable field collapses the first and last, which
#: would silently turn an axis-only correction into a suppression -- so "no opinion" has
#: its own value. It cannot collide with a real concept id, and `check_overrides` would
#: catch it if it somehow did.
KEEP_CONCEPT = "__keep_concept__"

#: An override may correct these axes. Deliberately every axis a spec carries,
#: including the defining ones: a human *may* say the classifier picked the wrong
#: form, which is exactly the correction an extractor is forbidden from making.
#: The asymmetry is the point -- a regex must not redefine an endpoint, a reviewer may.
OVERRIDABLE_AXES = (
    "endpoint_form",
    "measurement_concept",
    "reference_type",
    "direction",
    "scale_type",
    "summary_measure",
    "timepoint_anchor",
    "timepoint_selection",
    "threshold_kind",
    "threshold_operator",
    "analysis_population",
)


@dataclass(frozen=True)
class Override:
    outcome_uid: str
    reason: str
    reviewer: str
    decided_at: str
    concept_id: str | None = KEEP_CONCEPT
    axes: dict[str, str] = field(default_factory=dict)
    source_hash: str | None = None
    supersedes_concept_id: str | None = None
    status: str = "active"
    notes: str | None = None

    #: The reviewer decided no concept fits, actively suppressing a classification the
    #: engine would otherwise emit. Distinct from having no opinion about the concept.
    @property
    def suppresses(self) -> bool:
        return self.concept_id is None

    #: The reviewer named a concept, whether or not it differs from the derived one.
    @property
    def changes_concept(self) -> bool:
        return self.concept_id not in (None, KEEP_CONCEPT)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "outcome_uid": self.outcome_uid,
            "reason": self.reason,
            "reviewer": self.reviewer,
            "decided_at": self.decided_at,
        }
        # Absent means "no opinion"; explicit null means "nothing fits".
        if self.concept_id != KEEP_CONCEPT:
            record["concept_id"] = self.concept_id
        if self.axes:
            record["axes"] = dict(self.axes)
        if self.source_hash:
            record["source_hash"] = self.source_hash
        if self.supersedes_concept_id:
            record["supersedes_concept_id"] = self.supersedes_concept_id
        if self.status != "active":
            record["status"] = self.status
        if self.notes:
            record["notes"] = self.notes
        return record


def _validator() -> Draft202012Validator:
    schema = json.loads((PATHS.schemas / "overrides.schema.json").read_text())
    return Draft202012Validator(schema)


def load_overrides(path: Path | None = None) -> list[Override]:
    """Read and validate the override file. An absent file is simply no overrides."""
    target = path or PATHS.overrides
    if not target.exists():
        return []

    payload = yaml.safe_load(target.read_text()) or {"overrides": []}
    errors = sorted(_validator().iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        detail = "\n".join(
            f"  at {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:20]
        )
        raise OverrideError(f"{target} failed overrides.schema.json:\n{detail}")

    seen: set[str] = set()
    overrides: list[Override] = []
    for record in payload.get("overrides") or []:
        uid = record["outcome_uid"]
        if uid in seen:
            raise OverrideError(
                f"{target}: two overrides for {uid}. One outcome, one active decision -- "
                "retire the old one rather than stacking a second."
            )
        seen.add(uid)
        overrides.append(
            Override(
                outcome_uid=uid,
                reason=record["reason"],
                reviewer=record["reviewer"],
                decided_at=record["decided_at"],
                concept_id=record.get("concept_id", KEEP_CONCEPT),
                axes=dict(record.get("axes") or {}),
                source_hash=record.get("source_hash"),
                supersedes_concept_id=record.get("supersedes_concept_id"),
                status=record.get("status", "active"),
                notes=record.get("notes"),
            )
        )
    return overrides


def check_overrides(overrides: list[Override], vocab: Any) -> list[str]:
    """Cross-check overrides against the vocabulary. Returns human-readable problems.

    Run at validate time so a typo in a concept id fails in CI rather than silently
    producing an override that never applies.
    """
    problems: list[str] = []
    for override in overrides:
        if override.changes_concept and override.concept_id not in vocab.concepts:
            problems.append(
                f"{override.outcome_uid}: unknown concept '{override.concept_id}'"
            )
        if not override.changes_concept and not override.suppresses and not override.axes:
            problems.append(
                f"{override.outcome_uid}: names neither a concept nor an axis, so it "
                "would change nothing"
            )
        for axis_id, term_id in override.axes.items():
            if axis_id not in OVERRIDABLE_AXES:
                problems.append(
                    f"{override.outcome_uid}: '{axis_id}' is not an overridable axis"
                )
                continue
            axis = vocab.axes.get(axis_id)
            if axis is None:
                problems.append(f"{override.outcome_uid}: unknown axis '{axis_id}'")
            elif term_id not in axis.terms:
                problems.append(
                    f"{override.outcome_uid}: '{term_id}' is not a term of axis '{axis_id}'"
                )
    return problems


def save_overrides(overrides: list[Override], path: Path | None = None) -> Path:
    """Write the override file, sorted so diffs stay readable."""
    target = path or PATHS.overrides
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "description": (
            "Reviewer decisions amending derived endpoint specifications. "
            "Keyed by outcome so they survive re-derivation; source_hash makes them "
            "go stale when the registry text changes."
        ),
        "overrides": [o.to_dict() for o in sorted(overrides, key=lambda o: o.outcome_uid)],
    }
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100))
    return target


def record_override(
    conn: duckdb.DuckDBPyConnection,
    outcome_uid: str,
    reason: str,
    reviewer: str,
    concept_id: str | None = KEEP_CONCEPT,
    axes: dict[str, str] | None = None,
    notes: str | None = None,
    path: Path | None = None,
) -> Override:
    """Record a decision in both the YAML system of record and the database.

    The outcome's current hash and the classifier's current verdict are captured here
    rather than asked for, so the reviewer cannot accidentally pin a decision to the
    wrong text or lose what they were disagreeing with.
    """
    row = conn.execute(
        "SELECT record_hash FROM study_outcome WHERE outcome_uid = ?", [outcome_uid]
    ).fetchone()
    if row is None:
        raise OverrideError(f"no outcome '{outcome_uid}' in the database")

    prior = conn.execute(
        "SELECT concept_id FROM endpoint_spec WHERE outcome_uid = ?", [outcome_uid]
    ).fetchone()

    override = Override(
        outcome_uid=outcome_uid,
        reason=reason,
        reviewer=reviewer,
        decided_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        concept_id=concept_id,
        axes=dict(axes or {}),
        source_hash=row[0],
        supersedes_concept_id=prior[0] if prior else None,
        notes=notes,
    )

    existing = [o for o in load_overrides(path) if o.outcome_uid != outcome_uid]
    save_overrides(existing + [override], path)
    load_overrides_into_db(conn, path=path)
    return override


def load_overrides_into_db(
    conn: duckdb.DuckDBPyConnection, path: Path | None = None
) -> dict[str, int]:
    """Replace the override table from YAML, recomputing staleness against source text.

    Staleness is derived here rather than stored in the file because it is a fact about
    the relationship between a decision and current data, not a property of the
    decision. Recomputing on every load means an override cannot be left claiming to be
    current after the text beneath it moved.
    """
    overrides = load_overrides(path)
    conn.execute("DELETE FROM spec_override")

    counts = {"total": len(overrides), "active": 0, "stale": 0, "retired": 0, "orphaned": 0}

    for override in overrides:
        current = conn.execute(
            "SELECT record_hash FROM study_outcome WHERE outcome_uid = ?",
            [override.outcome_uid],
        ).fetchone()

        if override.status == "retired":
            status = "retired"
        elif current is None:
            # The outcome is not in this database -- a different scope, or a study that
            # left the registry. Not stale, just not applicable here.
            status = "stale"
            counts["orphaned"] += 1
        elif override.source_hash and current[0] != override.source_hash:
            status = "stale"
        else:
            status = "active"

        counts[status] = counts.get(status, 0) + 1
        conn.execute(
            """
            INSERT INTO spec_override (
                outcome_uid, concept_id, axes, reason, reviewer, decided_at,
                source_hash, supersedes_concept_id, status
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                override.outcome_uid,
                override.concept_id,
                _json(override.axes) if override.axes else None,
                override.reason,
                override.reviewer,
                override.decided_at,
                override.source_hash,
                override.supersedes_concept_id,
                status,
            ],
        )

    return counts


def active_overrides(conn: duckdb.DuckDBPyConnection) -> dict[str, Override]:
    """Overrides currently in force, keyed by outcome, for the classifier to apply."""
    rows = conn.execute(
        """
        SELECT outcome_uid, concept_id, axes, reason, reviewer, decided_at,
               source_hash, supersedes_concept_id, status
        FROM spec_override WHERE status = 'active'
        """
    ).fetchall()
    return {
        row[0]: Override(
            outcome_uid=row[0],
            concept_id=row[1],
            axes=json.loads(row[2]) if row[2] else {},
            reason=row[3],
            reviewer=row[4],
            decided_at=str(row[5]),
            source_hash=row[6],
            supersedes_concept_id=row[7],
            status=row[8],
        )
        for row in rows
    }
