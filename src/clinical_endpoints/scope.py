"""Study-level filters shared by every reporting command.

`--ta`, `--org`, `--phase`, `--drug-class` and `--since` mean the same thing
on `stats`, `results coverage`, `drug-class distribution` and the rest: each
narrows the set of pulled studies a report reads. Every option takes a
comma-separated list whose values are OR'd; different options are AND'd.
They match the semantics `pull` uses for the same flags, so a report filtered
with `--ta oncology --org Pfizer` reads the studies a pull with those flags
would have kept.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional, Sequence, Union

import duckdb

from clinical_endpoints.ingest.filters import PHASE_ALIASES

Values = Optional[tuple[str, ...]]


class ScopeError(ValueError):
    pass


def split_values(value: Union[None, str, Sequence[str]]) -> Values:
    """`"a, b"` or `["a", "b"]` -> `("a", "b")`; empty -> None."""
    if value is None:
        return None
    tokens = value.split(",") if isinstance(value, str) else list(value)
    out = tuple(t.strip() for t in tokens if t and t.strip())
    return out or None


def normalize_phase(raw: str) -> str:
    """Accept the `pull` shorthand (`3`, `2/3`, `na`) or the stored value
    (`PHASE3`, `PHASE2/PHASE3`)."""
    key = raw.strip().lower()
    if key in PHASE_ALIASES:
        return PHASE_ALIASES[key]
    stored = {v.lower(): v for v in PHASE_ALIASES.values()}
    if key in stored:
        return stored[key]
    valid = ", ".join(sorted(PHASE_ALIASES))
    raise ScopeError(f"Unrecognized phase {raw!r}. Valid values: {valid} (or PHASE3 etc.)")


def parse_since(raw: Optional[str]) -> Optional[dt.date]:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ScopeError(f"--since must be YYYY-MM-DD, got {raw!r}") from exc


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


@dataclass(frozen=True)
class StudyScope:
    # Any assigned therapeutic area (conformed.study_therapeutic_area), not
    # only the primary one, as `pull --ta` does.
    ta: Values = None
    # Case-insensitive substrings of the lead sponsor's name.
    org: Values = None
    # Stored phase values (PHASE3, PHASE2/PHASE3), matched exactly.
    phase: Values = None
    # Study tier (conformed.study_drug_class), any kind.
    drug_class: Values = None
    # raw.studies.start_date on or after this date.
    since: Optional[dt.date] = None

    @classmethod
    def from_options(
        cls,
        *,
        ta: Union[None, str, Sequence[str]] = None,
        org: Union[None, str, Sequence[str]] = None,
        phase: Union[None, str, Sequence[str]] = None,
        drug_class: Union[None, str, Sequence[str]] = None,
        since: Union[None, str, dt.date] = None,
    ) -> "StudyScope":
        phases = split_values(phase)
        return cls(
            ta=split_values(ta),
            org=split_values(org),
            phase=tuple(normalize_phase(p) for p in phases) if phases else None,
            drug_class=split_values(drug_class),
            since=parse_since(since) if isinstance(since, str) else since,
        )

    @property
    def is_empty(self) -> bool:
        return not (self.ta or self.org or self.phase or self.drug_class or self.since)

    def describe(self) -> str:
        parts = []
        for name, values in (
            ("ta", self.ta), ("org", self.org), ("phase", self.phase),
            ("drug_class", self.drug_class),
        ):
            if values:
                parts.append(f"{name}={','.join(values)}")
        if self.since:
            parts.append(f"since={self.since.isoformat()}")
        return ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "ta": list(self.ta) if self.ta else None,
            "org": list(self.org) if self.org else None,
            "phase": list(self.phase) if self.phase else None,
            "drug_class": list(self.drug_class) if self.drug_class else None,
            "since": self.since.isoformat() if self.since else None,
        }

    def validate(self, con: duckdb.DuckDBPyConnection) -> None:
        """Raise ScopeError when a filter names an unknown id or needs a table
        this warehouse has not built yet."""
        if self.is_empty:
            return
        if (self.org or self.phase or self.since) and not _table_exists(con, "raw", "studies"):
            raise ScopeError("--org/--phase/--since need raw.studies -- run `endpoints pull` first")
        if self.ta:
            if not _table_exists(con, "conformed", "study_therapeutic_area"):
                raise ScopeError(
                    "--ta needs conformed.study_therapeutic_area -- run `endpoints vocab "
                    "validate` and `endpoints pull` first"
                )
            if _table_exists(con, "vocab", "therapeutic_areas"):
                known = {r[0] for r in con.execute("SELECT id FROM vocab.therapeutic_areas").fetchall()}
                unknown = sorted(set(self.ta) - known)
                if unknown:
                    raise ScopeError(
                        f"--ta names unknown therapeutic area(s) {unknown}. Valid ids: {sorted(known)}"
                    )
        if self.drug_class:
            if not _table_exists(con, "conformed", "study_drug_class"):
                raise ScopeError(
                    "--drug-class needs conformed.study_drug_class -- run `endpoints vocab "
                    "validate` and `endpoints pull` first"
                )
            if _table_exists(con, "vocab", "drug_classes"):
                known = {r[0] for r in con.execute("SELECT id FROM vocab.drug_classes").fetchall()}
                unknown = sorted(set(self.drug_class) - known)
                if unknown:
                    raise ScopeError(
                        f"--drug-class names unknown drug class(es) {unknown}. See "
                        "`endpoints drug-class distribution` or vocab/drug_classes.yaml."
                    )

    def predicate(self, column: str = "nct_id") -> tuple[str, list]:
        """A SQL predicate over `column` (an nct_id) and its parameters.
        `("TRUE", [])` when the scope is empty."""
        clauses: list[str] = []
        params: list = []
        if self.ta:
            clauses.append(
                f"{column} IN (SELECT nct_id FROM conformed.study_therapeutic_area "
                "WHERE ta_id = ANY(?))"
            )
            params.append(list(self.ta))
        if self.drug_class:
            clauses.append(
                f"{column} IN (SELECT nct_id FROM conformed.study_drug_class "
                "WHERE drug_class_id = ANY(?))"
            )
            params.append(list(self.drug_class))
        study: list[str] = []
        if self.org:
            study.append(
                "(" + " OR ".join(["lower(organization) LIKE ?"] * len(self.org)) + ")"
            )
            params.extend(f"%{o.lower()}%" for o in self.org)
        if self.phase:
            study.append("phase = ANY(?)")
            params.append(list(self.phase))
        if self.since:
            study.append("start_date >= ?")
            params.append(self.since)
        if study:
            clauses.append(
                f"{column} IN (SELECT nct_id FROM raw.studies WHERE {' AND '.join(study)})"
            )
        return (" AND ".join(clauses) or "TRUE"), params

    def nct_ids(self, con: duckdb.DuckDBPyConnection) -> Optional[list[str]]:
        """The pulled studies in scope, or None when the scope is empty (every
        study). An empty list means the filters matched nothing."""
        if self.is_empty:
            return None
        if not _table_exists(con, "raw", "studies"):
            return []
        where, params = self.predicate("nct_id")
        return [
            row[0]
            for row in con.execute(
                f"SELECT nct_id FROM raw.studies WHERE {where} ORDER BY nct_id", params
            ).fetchall()
        ]
