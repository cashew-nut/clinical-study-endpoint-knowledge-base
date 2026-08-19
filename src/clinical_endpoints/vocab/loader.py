"""Load, validate, and materialise vocab/*.yaml into the warehouse `vocab.*` tables.

Plan section 4: "`vocab validate` loads and checks these into `vocab.*` DuckDB
tables (uniqueness of ids, no orphan synonyms)."

Checks beyond those two, all of which caught real defects while the vocabularies
were being written:

* every regex in every file compiles
* cross-file referential integrity -- a `default_scale` that names no scale, a
  `direction_by_ta` keyed on a therapeutic area that does not exist, a
  `match_precedence` list that has drifted out of step with the terms it orders
* closed value sets (direction_rule, domain, event_polarity, reference kind)
* a synonym claimed by two terms in the same dimension, which would make the
  match order silently decide the answer

Errors fail the command; warnings are reported and do not.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

from clinical_endpoints.vocab.schema import (
    DIMENSIONS,
    DIRECTION_RULES,
    EVENT_POLARITIES,
    MAPPING_FILENAME,
    MATCHING_FILENAME,
    MATCH_METHODS,
    CASCADE_FIELDS,
    MEASUREMENT_DOMAINS,
    REFERENCE_KINDS,
    VOCAB_DIRNAME,
    DimensionSpec,
)

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class VocabError(RuntimeError):
    """Raised when the vocabulary cannot be loaded at all (missing/unparseable file)."""


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    term_counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def default_vocab_dir(start: Path | str | None = None) -> Path:
    """Find the repo's `vocab/` directory, walking up from `start` (or cwd)."""
    here = Path(start) if start else Path.cwd()
    for candidate in (here, *here.parents):
        vocab_dir = candidate / VOCAB_DIRNAME
        if vocab_dir.is_dir():
            return vocab_dir
    # Fall back to the packaged location: src/clinical_endpoints/vocab/../../../vocab
    packaged = Path(__file__).resolve().parents[3] / VOCAB_DIRNAME
    if packaged.is_dir():
        return packaged
    raise VocabError(f"Could not find a {VOCAB_DIRNAME}/ directory from {here}")


def load_vocab(vocab_dir: Path | str) -> dict[str, Any]:
    """Parse every vocab file. Raises VocabError on a missing or unparseable file."""
    vocab_dir = Path(vocab_dir)
    docs: dict[str, Any] = {}
    for spec in DIMENSIONS:
        docs[spec.dimension] = _read_yaml(vocab_dir / spec.filename)
    docs["ta_mesh_mapping"] = _read_yaml(vocab_dir / MAPPING_FILENAME)
    docs["matching"] = _read_yaml(vocab_dir / MATCHING_FILENAME)
    return docs


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise VocabError(f"Missing vocab file: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise VocabError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise VocabError(f"{path.name} must be a mapping with a `terms:` key, got {type(doc).__name__}")
    return doc


# ---------------------------------------------------------------- validation


def validate_vocab(docs: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    ids: dict[str, set[str]] = {}

    for spec in DIMENSIONS:
        doc = docs[spec.dimension]
        ids[spec.dimension] = _validate_file_shape(spec, doc, result)
        result.term_counts[spec.dimension] = len(doc.get("terms") or [])

    for spec in DIMENSIONS:
        _validate_references(spec, docs[spec.dimension], ids, result)

    _validate_forms(docs["form"], ids, result)
    _validate_measurements(docs["measurement"], ids, result)
    _validate_references_file(docs["reference"], result)
    _validate_directions(docs["direction"], ids, result)
    _validate_scales(docs["scale"], result)
    _validate_therapeutic_areas(docs["therapeutic_area"], result)
    _validate_timepoints(docs["timepoint_pattern"], ids, result)
    _validate_ta_mesh_mapping(docs["ta_mesh_mapping"], ids, result)
    _validate_matching(docs["matching"], ids, result)
    return result


def _validate_file_shape(spec: DimensionSpec, doc: dict, result: ValidationResult) -> set[str]:
    where = spec.filename
    if doc.get("dimension") != spec.dimension:
        result.error(where, f"`dimension` must be {spec.dimension!r}, got {doc.get('dimension')!r}")
    if not isinstance(doc.get("version"), int):
        result.error(where, "`version` must be an integer")

    terms = doc.get("terms")
    if not isinstance(terms, list) or not terms:
        result.error(where, "`terms` must be a non-empty list")
        return set()

    seen: set[str] = set()
    synonym_owner: dict[str, str] = {}
    for i, term in enumerate(terms):
        if not isinstance(term, dict):
            result.error(where, f"term #{i} is not a mapping")
            continue
        term_id = term.get("id")
        if not isinstance(term_id, str) or not ID_RE.match(term_id):
            result.error(where, f"term #{i} has an invalid id {term_id!r} (expected snake_case)")
            continue
        if term_id in seen:
            result.error(where, f"duplicate term id {term_id!r}")
        seen.add(term_id)
        if not term.get("label"):
            result.error(where, f"{term_id}: missing `label`")

        for synonym in term.get("synonyms") or []:
            if not isinstance(synonym, str) or not synonym.strip():
                result.error(where, f"{term_id}: empty or non-string synonym")
                continue
            key = synonym.strip().lower()
            owner = synonym_owner.get(key)
            if owner and owner != term_id:
                result.error(
                    where,
                    f"synonym {synonym!r} is claimed by both {owner!r} and {term_id!r} -- "
                    "match order would silently decide which wins",
                )
            synonym_owner[key] = term_id

        for pattern in term.get("patterns") or []:
            _check_regex(where, f"{term_id} pattern", pattern, result)
        for pattern in term.get("not_if_matches") or []:
            _check_regex(where, f"{term_id} not_if_matches", pattern, result)

    return seen


def _check_regex(where: str, what: str, pattern: Any, result: ValidationResult) -> None:
    if not isinstance(pattern, str):
        result.error(where, f"{what}: expected a string, got {type(pattern).__name__}")
        return
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        result.error(where, f"{what}: does not compile ({exc}) -- {pattern!r}")


def _validate_references(
    spec: DimensionSpec, doc: dict, ids: dict[str, set[str]], result: ValidationResult
) -> None:
    for term in doc.get("terms") or []:
        if not isinstance(term, dict):
            continue
        term_id = term.get("id", "?")
        for field_name, target in spec.references.items():
            value = term.get(field_name)
            if value is not None and value not in ids.get(target, set()):
                result.error(spec.filename, f"{term_id}.{field_name} = {value!r} is not a {target} id")
        for field_name, target in spec.list_references.items():
            for value in term.get(field_name) or []:
                if value not in ids.get(target, set()):
                    result.error(
                        spec.filename, f"{term_id}.{field_name} contains {value!r}, not a {target} id"
                    )


def _validate_precedence(
    where: str, key: str, listed: list | None, term_ids: set[str], result: ValidationResult
) -> None:
    if listed is None:
        result.warn(where, f"no `{key}` -- match order will fall back to file order")
        return
    seen = set(listed)
    if len(seen) != len(listed):
        result.error(where, f"`{key}` contains duplicates")
    missing = term_ids - seen
    extra = seen - term_ids
    if missing:
        result.error(where, f"`{key}` omits {sorted(missing)}")
    if extra:
        result.error(where, f"`{key}` names unknown ids {sorted(extra)}")


def _validate_forms(doc: dict, ids: dict[str, set[str]], result: ValidationResult) -> None:
    where = "forms.yaml"
    form_ids = ids["form"]
    for term in doc.get("terms") or []:
        rule = term.get("direction_rule")
        if rule not in DIRECTION_RULES:
            result.error(where, f"{term.get('id')}: direction_rule {rule!r} not in {sorted(DIRECTION_RULES)}")
    _validate_precedence(where, "match_precedence", doc.get("match_precedence"), form_ids, result)

    fallback = doc.get("default_when_unmatched")
    if fallback not in form_ids:
        result.error(where, f"`default_when_unmatched` = {fallback!r} is not a form id")

    for rule in doc.get("disambiguation") or []:
        for form_id in list(rule.get("between") or []) + [rule.get("prefer"), rule.get("otherwise")]:
            if form_id is not None and form_id not in form_ids:
                result.error(where, f"disambiguation names unknown form {form_id!r}")


def _validate_measurements(doc: dict, ids: dict[str, set[str]], result: ValidationResult) -> None:
    where = "measurements.yaml"
    declared = set(doc.get("concepts") or [])
    used: set[str] = set()
    for term in doc.get("terms") or []:
        term_id = term.get("id", "?")
        concept = term.get("concept")
        if not concept:
            result.error(where, f"{term_id}: missing `concept`")
        else:
            used.add(concept)

        domain = term.get("domain")
        if domain not in MEASUREMENT_DOMAINS:
            result.error(where, f"{term_id}: domain {domain!r} not in {sorted(MEASUREMENT_DOMAINS)}")

        polarity = term.get("event_polarity")
        if polarity is not None and polarity not in EVENT_POLARITIES:
            result.error(where, f"{term_id}: event_polarity {polarity!r} not in {sorted(EVENT_POLARITIES)}")

        for ta_id, direction_id in (term.get("direction_by_ta") or {}).items():
            if ta_id not in ids["therapeutic_area"]:
                result.error(where, f"{term_id}.direction_by_ta keyed on unknown TA {ta_id!r}")
            if direction_id not in ids["direction"]:
                result.error(where, f"{term_id}.direction_by_ta[{ta_id}] = {direction_id!r} is not a direction id")

        score_range = term.get("score_range")
        if score_range is not None:
            if not (isinstance(score_range, list) and len(score_range) == 2):
                result.error(where, f"{term_id}: score_range must be [min, max]")
            elif score_range[0] >= score_range[1]:
                result.error(where, f"{term_id}: score_range min >= max")

        if term_id != "not_stated" and not term.get("default_direction") and not term.get("event_polarity"):
            result.warn(
                where,
                f"{term_id}: neither `default_direction` nor `event_polarity` -- Direction "
                "will derive to not_stated for every endpoint using it",
            )

    if used - declared:
        result.error(where, f"concepts used but not declared in `concepts:` {sorted(used - declared)}")
    if declared - used:
        result.error(where, f"concepts declared but unused {sorted(declared - used)}")

    if doc.get("on_unmatched") != "review_queue":
        result.error(where, "`on_unmatched` must be `review_queue` -- an unmatched measurement must never auto-conform")


def _validate_references_file(doc: dict, result: ValidationResult) -> None:
    where = "references.yaml"
    term_ids = {t["id"] for t in doc.get("terms") or [] if isinstance(t, dict) and "id" in t}
    for term in doc.get("terms") or []:
        kind = term.get("kind")
        if kind not in REFERENCE_KINDS:
            result.error(where, f"{term.get('id')}: kind {kind!r} not in {sorted(REFERENCE_KINDS)}")
    _validate_precedence(where, "match_precedence", doc.get("match_precedence"), term_ids, result)
    if doc.get("default_when_unmatched") not in term_ids:
        result.error(where, "`default_when_unmatched` is not a reference id")


def _validate_directions(doc: dict, ids: dict[str, set[str]], result: ValidationResult) -> None:
    where = "directions.yaml"
    for term in doc.get("terms") or []:
        sign = term.get("sign", "missing")
        if sign not in (-1, 0, 1, None):
            result.error(where, f"{term.get('id')}: sign must be -1, 0, 1 or null, got {sign!r}")
        for pattern in term.get("patterns") or []:
            _check_regex(where, f"{term.get('id')} pattern", pattern, result)

    cues = doc.get("event_polarity_cues") or {}
    for polarity in EVENT_POLARITIES:
        if polarity not in cues:
            result.warn(where, f"`event_polarity_cues` has no `{polarity}` list")
        for pattern in cues.get(polarity) or []:
            _check_regex(where, f"event_polarity_cues.{polarity}", pattern, result)

    if doc.get("default_when_underivable") not in ids["direction"]:
        result.error(where, "`default_when_underivable` is not a direction id")


def _validate_scales(doc: dict, result: ValidationResult) -> None:
    where = "scales.yaml"
    term_ids = {t["id"] for t in doc.get("terms") or [] if isinstance(t, dict) and "id" in t}
    for term in doc.get("terms") or []:
        si = term.get("si_equivalent")
        if si is not None and si not in term_ids:
            result.error(
                where,
                f"{term.get('id')}: si_equivalent {si!r} is not a scale id -- unit conversion "
                "within a kind needs a canonical unit that exists",
            )
        if term.get("factor_to_si") is not None and si is None:
            result.error(where, f"{term.get('id')}: factor_to_si without si_equivalent")
    if doc.get("default_when_unmatched") not in term_ids:
        result.error(where, "`default_when_unmatched` is not a scale id")


def _validate_therapeutic_areas(doc: dict, result: ValidationResult) -> None:
    where = "therapeutic_areas.yaml"
    precedences = [t.get("precedence") for t in doc.get("terms") or []]
    if any(p is None for p in precedences):
        result.error(where, "every therapeutic area needs a `precedence`")
    elif len(set(precedences)) != len(precedences):
        result.error(where, "`precedence` values must be unique -- ties would make the primary TA order-dependent")
    resolution = doc.get("resolution") or {}
    if resolution.get("primary_by") != "precedence":
        result.warn(where, "`resolution.primary_by` is not `precedence`; the loader assumes precedence ordering")


def _validate_timepoints(doc: dict, ids: dict[str, set[str]], result: ValidationResult) -> None:
    where = "timepoint_patterns.yaml"
    priorities = [t.get("priority") for t in doc.get("terms") or []]
    if any(p is None for p in priorities):
        result.error(where, "every timepoint pattern needs a `priority`")
    elif len(set(priorities)) != len(priorities):
        result.error(where, "`priority` values must be unique -- ties make classification order-dependent")
    for token, scale_id in (doc.get("unit_tokens") or {}).items():
        if scale_id not in ids["scale"]:
            result.error(where, f"unit_tokens[{token!r}] = {scale_id!r} is not a scale id")


def _validate_matching(doc: dict, ids: dict[str, set[str]], result: ValidationResult) -> None:
    """matching.yaml states rules the pipeline must implement; check they are
    internally consistent and that every id it names actually exists."""
    where = MATCHING_FILENAME
    for key in ("normalisation", "synonyms", "patterns", "precedence", "cascade", "provenance"):
        if key not in doc:
            result.errors.append(f"{where}: missing required section `{key}`")

    synonyms = doc.get("synonyms") or {}
    if synonyms.get("match") != "whole_token":
        result.errors.append(
            f"{where}: synonyms.match must be `whole_token`. Substring matching is what "
            f"assigned 9.8% of the corpus to `epistaxis_severity_score` via `ess` inside "
            f"'assessment'; the rule exists to stop that recurring."
        )
    minimum = (synonyms.get("case_sensitivity") or {}).get("min_synonym_length")
    if not isinstance(minimum, int) or minimum < 2:
        result.errors.append(
            f"{where}: synonyms.case_sensitivity.min_synonym_length must be an integer >= 2"
        )

    for dimension, steps in (doc.get("cascade") or {}).items():
        if dimension not in ids and dimension not in {"form", "measurement", "timepoint", "reference"}:
            result.errors.append(f"{where}: cascade names unknown dimension `{dimension}`")
        for step in steps:
            field = step.get("field")
            if field is not None and field not in CASCADE_FIELDS:
                result.errors.append(
                    f"{where}: cascade[{dimension}] reads unknown field `{field}`"
                )
            method = step.get("match_method")
            if method is not None and method not in MATCH_METHODS:
                result.errors.append(
                    f"{where}: cascade[{dimension}] uses unknown match_method `{method}`"
                )
        if steps and "fallback" not in steps[-1]:
            result.errors.append(
                f"{where}: cascade[{dimension}] must end in a `fallback` step, so an "
                f"unmatched string has a defined destination rather than a null"
            )

    floors = (doc.get("provenance") or {}).get("confidence_floor") or {}
    for method in doc.get("provenance", {}).get("match_method") or []:
        if method not in MATCH_METHODS:
            result.errors.append(f"{where}: provenance names unknown match_method `{method}`")
        elif method not in floors:
            result.errors.append(f"{where}: provenance.confidence_floor has no entry for `{method}`")
    if floors and floors.get("exact", 0) <= floors.get("syntactic_rule", 1):
        result.errors.append(
            f"{where}: provenance.confidence_floor must rank exact above syntactic_rule -- "
            f"a cascade hit from `description` is weaker evidence than the title saying it"
        )


def _validate_ta_mesh_mapping(doc: dict, ids: dict[str, set[str]], result: ValidationResult) -> None:
    where = MAPPING_FILENAME
    ta_ids = ids["therapeutic_area"]

    def check_ta(ta_id: Any, context: str) -> None:
        if ta_id not in ta_ids:
            result.error(where, f"{context} names unknown therapeutic area {ta_id!r}")

    for mesh_term, ta_id in (doc.get("term_overrides") or {}).items():
        check_ta(ta_id, f"term_overrides[{mesh_term!r}]")
    for prefix, ta_id in (doc.get("tree_prefixes") or {}).items():
        check_ta(ta_id, f"tree_prefixes[{prefix!r}]")
        if not re.match(r"^[A-Z]\d{2}(\.\d{3})*$", str(prefix)):
            result.warn(where, f"tree_prefixes key {prefix!r} does not look like a MeSH tree number")
    for rule in doc.get("term_patterns") or []:
        check_ta(rule.get("ta"), "term_patterns")
        for pattern in rule.get("patterns") or []:
            _check_regex(where, f"term_patterns[{rule.get('ta')}]", pattern, result)
    for rule in doc.get("intervention_rules") or []:
        check_ta(rule.get("ta"), "intervention_rules")
        for pattern in rule.get("term_patterns") or []:
            _check_regex(where, f"intervention_rules[{rule.get('ta')}]", pattern, result)
    for key, ta_id in (doc.get("defaults") or {}).items():
        check_ta(ta_id, f"defaults[{key}]")


# --------------------------------------------------------------- persistence

_LONG_TABLES = (
    ("synonyms", "dimension VARCHAR, term_id VARCHAR, synonym VARCHAR, synonym_normalised VARCHAR"),
    ("patterns", "dimension VARCHAR, term_id VARCHAR, pattern VARCHAR, pattern_role VARCHAR, ordinal INTEGER"),
    ("term_precedence", "dimension VARCHAR, term_id VARCHAR, rank INTEGER"),
    # Literal YAML file order, per term, per dimension -- matching.yaml's
    # longest-match tie-break is "the earlier term in file order", which is NOT
    # generally alphabetical by id (measurements.yaml is grouped by domain
    # section, e.g. tumour_burden_recist appears well before disease_recurrence
    # even though 'd' < 't'). This is the only reliable source for that order,
    # since SQL row order is otherwise not guaranteed.
    ("term_order", "dimension VARCHAR, term_id VARCHAR, ordinal INTEGER"),
    ("form_typical_reference", "form_id VARCHAR, reference_id VARCHAR"),
    ("form_typical_scale", "form_id VARCHAR, scale_id VARCHAR"),
    ("reference_implies_form", "reference_id VARCHAR, form_id VARCHAR"),
    ("measurement_typical_ta", "measurement_id VARCHAR, ta_id VARCHAR"),
    ("measurement_direction_by_ta", "measurement_id VARCHAR, ta_id VARCHAR, direction_id VARCHAR"),
    ("measurement_score_range", "measurement_id VARCHAR, score_min DOUBLE, score_max DOUBLE"),
    ("event_polarity_cues", "polarity VARCHAR, pattern VARCHAR"),
    ("ta_mesh_term_overrides", "mesh_term VARCHAR, mesh_term_normalised VARCHAR, ta_id VARCHAR"),
    ("ta_mesh_tree_prefixes", "tree_prefix VARCHAR, ta_id VARCHAR, prefix_length INTEGER"),
    ("ta_mesh_term_patterns", "ta_id VARCHAR, pattern VARCHAR, applies_to VARCHAR, ordinal INTEGER"),
    ("timepoint_unit_tokens", "token VARCHAR, scale_id VARCHAR"),
    ("timepoint_numeral_words", "word VARCHAR, value INTEGER"),
    ("timepoint_preprocessing", "ordinal INTEGER, step VARCHAR"),
    ("timepoint_abbreviations", "abbreviation VARCHAR, expansion VARCHAR"),
    ("timepoint_common_typos", "typo VARCHAR, correction VARCHAR"),
    ("timepoint_disambiguation", "ordinal INTEGER, between_forms VARCHAR[], prefer VARCHAR, otherwise VARCHAR, if_form_in VARCHAR[]"),
    ("form_disambiguation", "ordinal INTEGER, between_forms VARCHAR[], prefer VARCHAR, otherwise VARCHAR"),
    ("vocab_settings", "dimension VARCHAR, setting VARCHAR, value VARCHAR"),
    # matching.yaml -- the matching CONTRACT, persisted so the conforming pipeline
    # (build-order step 3) can read it from vocab.* like every other vocabulary
    # file, rather than re-parsing matching.yaml directly.
    ("matching_normalisation", "ordinal INTEGER, step VARCHAR"),
    ("matching_settings", "key VARCHAR, value VARCHAR"),
    ("matching_cascade", "dimension VARCHAR, ordinal INTEGER, field VARCHAR, match_method VARCHAR, fallback_value VARCHAR"),
    ("matching_confidence_floor", "match_method VARCHAR, confidence DOUBLE"),
)


def write_vocab_tables(
    con: duckdb.DuckDBPyConnection, docs: dict[str, Any], *, vocab_dir: Path | str
) -> dict[str, int]:
    """Replace every `vocab.*` table from `docs`. Returns row counts per table.

    Wholesale replacement, matching how `pull` refreshes `raw.*`: re-running
    `vocab validate` is a refresh, not an append.
    """
    vocab_dir = Path(vocab_dir)
    con.execute("CREATE SCHEMA IF NOT EXISTS vocab")
    counts: dict[str, int] = {}

    for spec in DIMENSIONS:
        doc = docs[spec.dimension]
        cols = ", ".join(f"{c} VARCHAR" if c not in _NUMERIC_COLUMNS else f"{c} DOUBLE" for c in spec.columns)
        con.execute(f"CREATE OR REPLACE TABLE vocab.{spec.table} ({cols})")
        rows = [
            tuple(_scalar(term.get(c, _BOOLEAN_DEFAULTS.get(c))) for c in spec.columns)
            for term in doc["terms"]
        ]
        _insert(con, f"vocab.{spec.table}", len(spec.columns), rows)
        counts[spec.table] = len(rows)

    for name, ddl in _LONG_TABLES:
        con.execute(f"CREATE OR REPLACE TABLE vocab.{name} ({ddl})")

    counts.update(_write_long_tables(con, docs))
    counts["_load_log"] = _write_load_log(con, docs, vocab_dir)
    return counts


_NUMERIC_COLUMNS = frozenset({"sign", "precedence", "priority", "factor_to_si", "mcid"})

# Boolean term flags, with the value assumed when a term omits them. Written out
# explicitly so `WHERE analysable = 'true'` works without a COALESCE.
_BOOLEAN_DEFAULTS = {"analysable": True, "expects_threshold": False, "composite": False}


def _scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        return None
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _insert(con: duckdb.DuckDBPyConnection, table: str, n_cols: int, rows: list[tuple]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["?"] * n_cols)
    con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def normalise(text: str) -> str:
    """Shared normalisation for synonym and MeSH-term lookup keys."""
    return " ".join(text.strip().lower().split())


def _write_long_tables(con: duckdb.DuckDBPyConnection, docs: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    synonyms: list[tuple] = []
    patterns: list[tuple] = []
    precedence: list[tuple] = []
    settings: list[tuple] = []
    term_order: list[tuple] = []

    for spec in DIMENSIONS:
        doc = docs[spec.dimension]
        for i, term in enumerate(doc["terms"]):
            term_order.append((spec.dimension, term["id"], i))
        for term in doc["terms"]:
            term_id = term["id"]
            for synonym in term.get("synonyms") or []:
                synonyms.append((spec.dimension, term_id, synonym, normalise(synonym)))
            for i, pattern in enumerate(term.get("patterns") or []):
                patterns.append((spec.dimension, term_id, pattern, "match", i))
            for i, pattern in enumerate(term.get("not_if_matches") or []):
                patterns.append((spec.dimension, term_id, pattern, "guard", i))
        for i, term_id in enumerate(doc.get("match_precedence") or []):
            precedence.append((spec.dimension, term_id, i))
        for key in ("default_when_unmatched", "default_when_underivable", "on_unmatched"):
            if key in doc:
                settings.append((spec.dimension, key, _scalar(doc[key])))

    # Timepoint order is `priority`, not a match_precedence list.
    for term in sorted(docs["timepoint_pattern"]["terms"], key=lambda t: t["priority"]):
        precedence.append(("timepoint_pattern", term["id"], term["priority"]))
    # Therapeutic-area order is `precedence`.
    for term in sorted(docs["therapeutic_area"]["terms"], key=lambda t: t["precedence"]):
        precedence.append(("therapeutic_area", term["id"], term["precedence"]))

    _insert(con, "vocab.synonyms", 4, synonyms)
    _insert(con, "vocab.patterns", 5, patterns)
    _insert(con, "vocab.term_precedence", 3, precedence)
    _insert(con, "vocab.vocab_settings", 3, settings)
    _insert(con, "vocab.term_order", 3, term_order)
    counts.update(synonyms=len(synonyms), patterns=len(patterns), term_precedence=len(precedence),
                  vocab_settings=len(settings), term_order=len(term_order))

    forms, measurements, references = docs["form"], docs["measurement"], docs["reference"]

    ftr = [(t["id"], r) for t in forms["terms"] for r in t.get("typical_reference") or []]
    fts = [(t["id"], s) for t in forms["terms"] for s in t.get("typical_scale") or []]
    rif = [(t["id"], f) for t in references["terms"] for f in t.get("implies_form") or []]
    mta = [(t["id"], a) for t in measurements["terms"] for a in t.get("typical_ta") or []]
    mdt = [(t["id"], a, d) for t in measurements["terms"] for a, d in (t.get("direction_by_ta") or {}).items()]
    msr = [(t["id"], *t["score_range"]) for t in measurements["terms"] if t.get("score_range")]

    _insert(con, "vocab.form_typical_reference", 2, ftr)
    _insert(con, "vocab.form_typical_scale", 2, fts)
    _insert(con, "vocab.reference_implies_form", 2, rif)
    _insert(con, "vocab.measurement_typical_ta", 2, mta)
    _insert(con, "vocab.measurement_direction_by_ta", 3, mdt)
    _insert(con, "vocab.measurement_score_range", 3, msr)
    counts.update(form_typical_reference=len(ftr), form_typical_scale=len(fts),
                  reference_implies_form=len(rif), measurement_typical_ta=len(mta),
                  measurement_direction_by_ta=len(mdt), measurement_score_range=len(msr))

    cues = [
        (polarity, pattern)
        for polarity in EVENT_POLARITIES
        for pattern in (docs["direction"].get("event_polarity_cues") or {}).get(polarity) or []
    ]
    _insert(con, "vocab.event_polarity_cues", 2, cues)
    counts["event_polarity_cues"] = len(cues)

    tp = docs["timepoint_pattern"]
    tokens = [(k, v) for k, v in (tp.get("unit_tokens") or {}).items()]
    numerals = [(k, int(v)) for k, v in (tp.get("numeral_words") or {}).items()]
    _insert(con, "vocab.timepoint_unit_tokens", 2, tokens)
    _insert(con, "vocab.timepoint_numeral_words", 2, numerals)
    counts.update(timepoint_unit_tokens=len(tokens), timepoint_numeral_words=len(numerals))

    mapping = docs["ta_mesh_mapping"]
    overrides = [(t, normalise(t), ta) for t, ta in (mapping.get("term_overrides") or {}).items()]
    prefixes = [(p, ta, len(str(p))) for p, ta in (mapping.get("tree_prefixes") or {}).items()]
    ta_patterns = [
        (rule["ta"], pattern, "condition", i)
        for rule in mapping.get("term_patterns") or []
        for i, pattern in enumerate(rule.get("patterns") or [])
    ] + [
        (rule["ta"], pattern, "intervention", i)
        for rule in mapping.get("intervention_rules") or []
        for i, pattern in enumerate(rule.get("term_patterns") or [])
    ]
    _insert(con, "vocab.ta_mesh_term_overrides", 3, overrides)
    _insert(con, "vocab.ta_mesh_tree_prefixes", 3, prefixes)
    _insert(con, "vocab.ta_mesh_term_patterns", 4, ta_patterns)
    counts.update(ta_mesh_term_overrides=len(overrides), ta_mesh_tree_prefixes=len(prefixes),
                  ta_mesh_term_patterns=len(ta_patterns))

    # timepoint_patterns.yaml fields with no scalar column of their own: the
    # preprocessing order, the abbreviation/typo expansion tables, and the
    # disambiguation block (build-order step 3 must honour all three).
    tp_preprocessing = [(i, step) for i, step in enumerate(tp.get("preprocessing") or [])]
    tp_abbreviations = [(k, v) for k, v in (tp.get("abbreviations") or {}).items()]
    tp_typos = [(k, v) for k, v in (tp.get("common_typos") or {}).items()]
    tp_disambiguation = [
        (i, list(rule.get("between") or []), rule.get("prefer"), rule.get("otherwise"),
         list(rule.get("if_form_in") or []))
        for i, rule in enumerate(tp.get("disambiguation") or [])
    ]
    _insert(con, "vocab.timepoint_preprocessing", 2, tp_preprocessing)
    _insert(con, "vocab.timepoint_abbreviations", 2, tp_abbreviations)
    _insert(con, "vocab.timepoint_common_typos", 2, tp_typos)
    _insert(con, "vocab.timepoint_disambiguation", 5, tp_disambiguation)
    counts.update(timepoint_preprocessing=len(tp_preprocessing), timepoint_abbreviations=len(tp_abbreviations),
                  timepoint_common_typos=len(tp_typos), timepoint_disambiguation=len(tp_disambiguation))

    # forms.yaml's disambiguation block -- e.g. responder_proportion vs
    # incidence_proportion, decided by the matched measurement's event_polarity
    # rather than by wording (see vocab/README.md decision #4).
    form_disambiguation = [
        (i, list(rule.get("between") or []), rule.get("prefer"), rule.get("otherwise"))
        for i, rule in enumerate(forms.get("disambiguation") or [])
    ]
    _insert(con, "vocab.form_disambiguation", 4, form_disambiguation)
    counts["form_disambiguation"] = len(form_disambiguation)

    counts.update(_write_matching_tables(con, docs["matching"]))
    return counts


def _write_matching_tables(con: duckdb.DuckDBPyConnection, matching: dict[str, Any]) -> dict[str, int]:
    """Persist matching.yaml -- the contract for HOW every other vocab file is
    matched -- into vocab.* tables, so the conforming pipeline reads it the same
    way it reads every term file: from the warehouse, never by re-parsing YAML.
    """
    normalisation = [(i, step) for i, step in enumerate(matching.get("normalisation") or [])]

    synonyms = matching.get("synonyms") or {}
    case_sensitivity = synonyms.get("case_sensitivity") or {}
    patterns = matching.get("patterns") or {}
    precedence = matching.get("precedence") or {}
    settings = [
        ("synonyms.match", synonyms.get("match")),
        ("synonyms.internal_space_matches", synonyms.get("internal_space_matches")),
        ("synonyms.case_sensitivity.rule", case_sensitivity.get("rule")),
        ("synonyms.case_sensitivity.min_synonym_length", case_sensitivity.get("min_synonym_length")),
        ("patterns.default_flags", ",".join(patterns.get("default_flags") or [])),
        ("patterns.inline_case_sensitive_syntax", patterns.get("inline_case_sensitive_syntax")),
        ("precedence.strategy", precedence.get("strategy")),
        ("precedence.strategy_when_unordered", precedence.get("strategy_when_unordered")),
        ("precedence.synonyms_before_patterns", precedence.get("synonyms_before_patterns")),
        ("precedence.veto_field", precedence.get("veto_field")),
    ]
    settings = [(k, str(_scalar(v))) for k, v in settings if v is not None]

    cascade: list[tuple] = []
    for dimension, steps in (matching.get("cascade") or {}).items():
        for i, step in enumerate(steps):
            cascade.append((dimension, i, step.get("field"), step.get("match_method"), step.get("fallback")))

    floors = ((matching.get("provenance") or {}).get("confidence_floor")) or {}
    confidence_floor = [(method, float(value)) for method, value in floors.items()]

    _insert(con, "vocab.matching_normalisation", 2, normalisation)
    _insert(con, "vocab.matching_settings", 2, settings)
    _insert(con, "vocab.matching_cascade", 5, cascade)
    _insert(con, "vocab.matching_confidence_floor", 2, confidence_floor)
    return {
        "matching_normalisation": len(normalisation),
        "matching_settings": len(settings),
        "matching_cascade": len(cascade),
        "matching_confidence_floor": len(confidence_floor),
    }


def _write_load_log(con: duckdb.DuckDBPyConnection, docs: dict[str, Any], vocab_dir: Path) -> int:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS vocab._load_log (
            loaded_at TIMESTAMP, vocab_dir VARCHAR, file VARCHAR,
            dimension VARCHAR, version INTEGER, term_count INTEGER, sha256 VARCHAR
        )
        """
    )
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows = []
    for spec in DIMENSIONS:
        path = vocab_dir / spec.filename
        doc = docs[spec.dimension]
        rows.append((now, str(vocab_dir), spec.filename, spec.dimension, doc["version"],
                     len(doc["terms"]), _sha256(path)))
    mapping_path = vocab_dir / MAPPING_FILENAME
    rows.append((now, str(vocab_dir), MAPPING_FILENAME, "ta_mesh_mapping",
                 docs["ta_mesh_mapping"].get("version"),
                 len(docs["ta_mesh_mapping"].get("term_overrides") or {}), _sha256(mapping_path)))
    _insert(con, "vocab._load_log", 7, rows)
    return len(rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
