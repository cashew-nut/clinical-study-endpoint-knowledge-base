"""Load and validate the controlled vocabularies, concepts and rule packs.

Everything downstream reads vocabulary through this module, so validation happens
exactly once and no component can quietly depend on a term that does not exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator, RefResolver

from ceskb.config import PATHS


class VocabularyError(Exception):
    """Raised when the vocabulary set is internally inconsistent."""


#: Maps a key of a concept's ``structure`` block to the axis its value is drawn from,
#: and to the role that axis plays for the concept.
#:
#: ``defining`` axes are what make the concept the concept: change one and you have a
#: different endpoint. ``default`` axes carry the usual choice, which a protocol may
#: legitimately override in Layer B without becoming a different endpoint.
STRUCTURE_AXES: dict[str, tuple[str, str]] = {
    "endpoint_form": ("endpoint_form", "defining"),
    "measurement_concept": ("measurement_concept", "defining"),
    "reference_type": ("reference_type", "defining"),
    "direction": ("direction", "defining"),
    "scale_type": ("scale_type", "defining"),
    "default_summary_measure": ("summary_measure", "default"),
    "default_timepoint_selection": ("timepoint_selection", "default"),
}


# --------------------------------------------------------------------------- #
# schema plumbing
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=None)
def _schema_store() -> dict[str, Any]:
    store: dict[str, Any] = {}
    for path in PATHS.schemas.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        store[path.name] = schema
        if "$id" in schema:
            store[schema["$id"]] = schema
    return store


def _validator(schema_name: str) -> Draft202012Validator:
    store = _schema_store()
    schema = store[schema_name]
    # concept.schema.json references axis.schema.json by relative filename.
    resolver = RefResolver(base_uri="", referrer=schema, store=store)
    return Draft202012Validator(schema, resolver=resolver)


def _validate(instance: Any, schema_name: str, source: Path) -> None:
    errors = sorted(_validator(schema_name).iter_errors(instance), key=lambda e: e.path)
    if errors:
        detail = "\n".join(
            f"  at {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:20]
        )
        raise VocabularyError(f"{source} failed {schema_name}:\n{detail}")


# --------------------------------------------------------------------------- #
# data classes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Term:
    axis_id: str
    term_id: str
    label: str
    definition: str
    synonyms: tuple[str, ...] = ()
    broader: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    external_mappings: tuple[dict[str, Any], ...] = ()
    status: str = "draft"
    notes: str | None = None

    @property
    def key(self) -> str:
        return f"{self.axis_id}:{self.term_id}"


@dataclass(frozen=True)
class Axis:
    axis_id: str
    label: str
    definition: str
    extensible: bool
    terms: dict[str, Term]
    usdm_alignment: dict[str, Any] = field(default_factory=dict)
    references: tuple[dict[str, Any], ...] = ()

    def term(self, term_id: str) -> Term:
        try:
            return self.terms[term_id]
        except KeyError as exc:
            raise VocabularyError(
                f"'{term_id}' is not a term in axis '{self.axis_id}'. "
                f"Known terms: {', '.join(sorted(self.terms))}"
            ) from exc


@dataclass(frozen=True)
class Concept:
    concept_id: str
    label: str
    definition: str
    therapeutic_areas: tuple[str, ...]
    structure: dict[str, str]
    abbreviation: str | None = None
    synonyms: tuple[str, ...] = ()
    definitional_threshold: dict[str, Any] | None = None
    typical_units: tuple[str, ...] = ()
    governing_criteria: tuple[dict[str, Any], ...] = ()
    components: tuple[dict[str, Any], ...] = ()
    related_concepts: tuple[dict[str, Any], ...] = ()
    external_mappings: tuple[dict[str, Any], ...] = ()
    usdm_hints: dict[str, Any] = field(default_factory=dict)
    status: str = "draft"
    notes: str | None = None
    source_file: str = ""


@dataclass(frozen=True)
class Rule:
    rulepack_id: str
    rulepack_version: str
    rule_id: str
    concept_id: str
    priority: int
    confidence: float
    therapeutic_area_hint: str | None
    match: dict[str, Any]
    asserts: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None

    @property
    def qualified_id(self) -> str:
        return f"{self.rulepack_id}@{self.rulepack_version}#{self.rule_id}"


@dataclass(frozen=True)
class Vocabulary:
    axes: dict[str, Axis]
    concepts: dict[str, Concept]
    rules: tuple[Rule, ...]

    def axis(self, axis_id: str) -> Axis:
        try:
            return self.axes[axis_id]
        except KeyError as exc:
            raise VocabularyError(f"unknown axis '{axis_id}'") from exc

    def concept(self, concept_id: str) -> Concept:
        try:
            return self.concepts[concept_id]
        except KeyError as exc:
            raise VocabularyError(f"unknown concept '{concept_id}'") from exc

    def terms(self) -> Iterable[Term]:
        for axis in self.axes.values():
            yield from axis.terms.values()


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> Any:
    with path.open() as handle:
        return yaml.safe_load(handle)


def load_axes(directory: Path | None = None) -> dict[str, Axis]:
    directory = directory or PATHS.axes
    axes: dict[str, Axis] = {}
    for path in sorted(directory.glob("*.yaml")):
        raw = _load_yaml(path)
        _validate(raw, "axis.schema.json", path)
        terms: dict[str, Term] = {}
        for entry in raw["terms"]:
            if entry["term_id"] in terms:
                raise VocabularyError(f"{path}: duplicate term '{entry['term_id']}'")
            terms[entry["term_id"]] = Term(
                axis_id=raw["axis_id"],
                term_id=entry["term_id"],
                label=entry["label"],
                definition=entry["definition"],
                synonyms=tuple(entry.get("synonyms", [])),
                broader=entry.get("broader"),
                attributes=entry.get("attributes", {}) or {},
                external_mappings=tuple(entry.get("external_mappings", [])),
                status=entry.get("status", "draft"),
                notes=entry.get("notes"),
            )
        axis = Axis(
            axis_id=raw["axis_id"],
            label=raw["label"],
            definition=raw["definition"],
            extensible=raw["extensible"],
            terms=terms,
            usdm_alignment=raw.get("usdm_alignment", {}) or {},
            references=tuple(raw.get("references", [])),
        )
        if axis.axis_id in axes:
            raise VocabularyError(f"{path}: axis '{axis.axis_id}' defined twice")
        axes[axis.axis_id] = axis

    # broader must resolve inside its own axis
    for axis in axes.values():
        for term in axis.terms.values():
            if term.broader and term.broader not in axis.terms:
                raise VocabularyError(
                    f"axis '{axis.axis_id}' term '{term.term_id}' has broader "
                    f"'{term.broader}', which is not a term in the same axis"
                )
    return axes


def load_concepts(directory: Path | None = None) -> dict[str, Concept]:
    directory = directory or PATHS.concepts
    concepts: dict[str, Concept] = {}
    for path in sorted(directory.glob("*.yaml")):
        raw = _load_yaml(path)
        entries = raw["concepts"] if isinstance(raw, dict) and "concepts" in raw else [raw]
        for entry in entries:
            _validate(entry, "concept.schema.json", path)
            concept = Concept(
                concept_id=entry["concept_id"],
                label=entry["label"],
                definition=entry["definition"],
                therapeutic_areas=tuple(entry["therapeutic_areas"]),
                structure=dict(entry["structure"]),
                abbreviation=entry.get("abbreviation"),
                synonyms=tuple(entry.get("synonyms", [])),
                definitional_threshold=entry.get("definitional_threshold"),
                typical_units=tuple(entry.get("typical_units", [])),
                governing_criteria=tuple(entry.get("governing_criteria", [])),
                components=tuple(entry.get("components", [])),
                related_concepts=tuple(entry.get("related_concepts", [])),
                external_mappings=tuple(entry.get("external_mappings", [])),
                usdm_hints=entry.get("usdm_hints", {}) or {},
                status=entry.get("status", "draft"),
                notes=entry.get("notes"),
                source_file=path.name,
            )
            if concept.concept_id in concepts:
                raise VocabularyError(f"{path}: concept '{concept.concept_id}' defined twice")
            concepts[concept.concept_id] = concept
    return concepts


def load_rules(directory: Path | None = None) -> tuple[Rule, ...]:
    directory = directory or PATHS.rules
    rules: list[Rule] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        raw = _load_yaml(path)
        _validate(raw, "rulepack.schema.json", path)
        for entry in raw["rules"]:
            rule = Rule(
                rulepack_id=raw["rulepack_id"],
                rulepack_version=raw["version"],
                rule_id=entry["rule_id"],
                concept_id=entry["concept_id"],
                priority=entry.get("priority", 100),
                confidence=entry.get("confidence", 0.8),
                therapeutic_area_hint=entry.get("therapeutic_area_hint"),
                match=entry["match"],
                asserts=entry.get("asserts", {}) or {},
                notes=entry.get("notes"),
            )
            if rule.qualified_id in seen:
                raise VocabularyError(f"{path}: duplicate rule '{rule.qualified_id}'")
            seen.add(rule.qualified_id)
            rules.append(rule)
    return tuple(rules)


def _check_referential_integrity(vocab: Vocabulary) -> None:
    """Every cross-reference in a concept or rule must resolve to something real.

    This is the guard that makes the vocabulary usable as a contract rather than as
    documentation: a typo in an axis term fails the load instead of silently producing
    an endpoint with no structure.
    """
    problems: list[str] = []

    for concept in vocab.concepts.values():
        where = f"concept '{concept.concept_id}'"
        for key, term_id in concept.structure.items():
            if key not in STRUCTURE_AXES:
                problems.append(f"{where}: structure key '{key}' has no axis mapping")
                continue
            axis_id, _role = STRUCTURE_AXES[key]
            if axis_id not in vocab.axes:
                problems.append(f"{where}: structure.{key} maps to unknown axis '{axis_id}'")
            elif term_id not in vocab.axes[axis_id].terms:
                problems.append(
                    f"{where}: structure.{key} = '{term_id}' is not a term in axis '{axis_id}'"
                )

        ta_axis = vocab.axes.get("therapeutic_area")
        for ta in concept.therapeutic_areas:
            if ta_axis and ta not in ta_axis.terms:
                problems.append(f"{where}: unknown therapeutic area '{ta}'")

        unit_axis = vocab.axes.get("unit")
        for unit in concept.typical_units:
            if unit_axis and unit not in unit_axis.terms:
                problems.append(f"{where}: unknown unit '{unit}'")

        threshold = concept.definitional_threshold
        if threshold:
            kind_axis = vocab.axes.get("threshold_kind")
            op_axis = vocab.axes.get("threshold_operator")
            if kind_axis and threshold["kind"] not in kind_axis.terms:
                problems.append(f"{where}: unknown threshold kind '{threshold['kind']}'")
            if op_axis and threshold["operator"] not in op_axis.terms:
                problems.append(f"{where}: unknown threshold operator '{threshold['operator']}'")
            unit = threshold.get("unit")
            if unit and unit_axis and unit not in unit_axis.terms:
                problems.append(f"{where}: unknown threshold unit '{unit}'")

        for related in concept.related_concepts:
            if related["concept_id"] not in vocab.concepts:
                problems.append(
                    f"{where}: related_concepts references unknown concept "
                    f"'{related['concept_id']}'"
                )

        mc_axis = vocab.axes.get("measurement_concept")
        for component in concept.components:
            mc = component.get("measurement_concept")
            if mc and mc_axis and mc not in mc_axis.terms:
                problems.append(f"{where}: component references unknown measurement '{mc}'")
            cid = component.get("concept_id")
            if cid and cid not in vocab.concepts:
                problems.append(f"{where}: component references unknown concept '{cid}'")

    for rule in vocab.rules:
        where = f"rule '{rule.qualified_id}'"
        if rule.concept_id not in vocab.concepts:
            problems.append(f"{where}: unknown concept '{rule.concept_id}'")
        hint = rule.therapeutic_area_hint
        ta_axis = vocab.axes.get("therapeutic_area")
        if hint and ta_axis and hint not in ta_axis.terms:
            problems.append(f"{where}: unknown therapeutic_area_hint '{hint}'")
        for axis_id, term_id in rule.asserts.items():
            if axis_id in vocab.axes and isinstance(term_id, str):
                if term_id not in vocab.axes[axis_id].terms:
                    problems.append(f"{where}: asserts.{axis_id} = '{term_id}' is not a term")

    if problems:
        raise VocabularyError(
            "vocabulary failed referential integrity checks:\n  - "
            + "\n  - ".join(problems)
        )


@lru_cache(maxsize=1)
def load_vocabulary() -> Vocabulary:
    """Load, validate and cross-check the whole vocabulary set."""
    vocab = Vocabulary(
        axes=load_axes(),
        concepts=load_concepts(),
        rules=load_rules(),
    )
    _check_referential_integrity(vocab)
    return vocab


def reload_vocabulary() -> Vocabulary:
    load_vocabulary.cache_clear()
    _schema_store.cache_clear()
    return load_vocabulary()
