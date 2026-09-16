"""Project the conformed warehouse into USDM 4.0 endpoint-module objects.

A rendering step, not an inference step: every dimension was resolved by
`conform`, and this turns those decisions into `Objective` / `Endpoint` /
`SyntaxTemplateDictionary` / `BiomedicalConceptSurrogate` /
`AnalysisPopulation` instances.

Every row in `raw.design_outcomes` for the NCT id becomes exactly one USDM
`Endpoint`, at one of three fidelity tiers, so a study whose endpoints did not
conform never appears to have fewer endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from typing import Any, Iterable

import duckdb

from clinical_endpoints.usdm import codes
from clinical_endpoints.usdm.ids import IdFactory, usdm_ref
from clinical_endpoints.usdm.tags import (
    HOST_REFERENCE_ATTRIBUTE,
    TAG_HOSTS,
    TagValue,
    render_threshold,
    render_timepoint,
)
from clinical_endpoints.usdm.templates import Rendered, parse_template, render

# Term ids that mean "absent". A tag resolving to one of these is unresolved.
UNRESOLVED_TERM_IDS = frozenset({"none", "not_stated", "", None})

TIER_TEMPLATED = "templated"
TIER_PARTIAL = "partial"
TIER_VERBATIM = "verbatim"


class NotPulled(LookupError):
    pass


class NotConformed(RuntimeError):
    pass


@dataclass(frozen=True)
class TemplateSpec:
    form_id: str
    template: str | None
    verbatim: bool
    reference_fallback: str | None
    parts: tuple = ()


@dataclass(frozen=True)
class ProjectionRules:
    templates: dict[str, TemplateSpec]
    purposes: dict[str, str]
    objective_templates: dict[str, str]
    concept_list_limit: int
    inline: dict[str, dict[str, str]]
    definitions: dict[str, dict[str, str]]
    measurement_concept: dict[str, str]
    measurement_domain: dict[str, str]
    event_family: dict[str, bool]
    event_concept: dict[str, str]
    timepoint_role: dict[str, str] = field(default_factory=dict)
    vocab_version: str | None = None

    def inline_label(self, dimension: str, term_id: str | None) -> str | None:
        if term_id in UNRESOLVED_TERM_IDS:
            return None
        return self.inline.get(dimension, {}).get(term_id) or None


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def _inline_map(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    """`inline_label` where the term declares one, else `label`."""
    rows = con.execute(f"SELECT id, coalesce(inline_label, label) FROM vocab.{table}").fetchall()
    return {term_id: value for term_id, value in rows if value}


def load_projection_rules(con: duckdb.DuckDBPyConnection) -> ProjectionRules:
    if not _table_exists(con, "vocab", "usdm_templates"):
        raise NotConformed(
            "vocab.usdm_templates is empty -- run `endpoints vocab validate` first"
        )

    templates: dict[str, TemplateSpec] = {}
    for form_id, template, verbatim, fallback, _req, _all in con.execute(
        "SELECT form_id, template, verbatim, reference_fallback, required_tags, all_tags "
        "FROM vocab.usdm_templates"
    ).fetchall():
        templates[form_id] = TemplateSpec(
            form_id=form_id,
            template=template,
            verbatim=bool(verbatim),
            reference_fallback=fallback,
            parts=parse_template(template) if template else (),
        )

    purposes = dict(con.execute("SELECT domain, purpose FROM vocab.usdm_purposes").fetchall())
    objective_templates = dict(
        con.execute("SELECT level, template FROM vocab.usdm_objective_templates").fetchall()
    )
    settings = dict(con.execute("SELECT key, value FROM vocab.usdm_settings").fetchall())

    measurements = con.execute(
        "SELECT id, concept, domain, definition FROM vocab.measurements"
    ).fetchall()
    event_family = {
        row[0]: (row[1] == "true") for row in con.execute("SELECT id, event_family FROM vocab.forms").fetchall()
    }
    event_concept = {
        row[0]: row[1] for row in con.execute("SELECT id, concept FROM vocab.events").fetchall() if row[1]
    }
    timepoint_role = {
        row[0]: row[1] for row in con.execute("SELECT id, role FROM vocab.timepoint_patterns").fetchall() if row[1]
    }

    return ProjectionRules(
        templates=templates,
        purposes=purposes,
        objective_templates=objective_templates,
        concept_list_limit=int(settings.get("concept_list_limit", 3)),
        inline={
            "measurement": _inline_map(con, "measurements"),
            "reference": _inline_map(con, "references"),
            "scale": _inline_map(con, "scales"),
            "event": _inline_map(con, "events"),
        },
        definitions={
            "measurement": {m[0]: m[3] for m in measurements if m[3]},
        },
        measurement_concept={m[0]: m[1] for m in measurements if m[1]},
        measurement_domain={m[0]: m[2] for m in measurements if m[2]},
        event_family=event_family,
        event_concept=event_concept,
        timepoint_role=timepoint_role,
        vocab_version=_vocab_version(con),
    )


def _vocab_version(con: duckdb.DuckDBPyConnection) -> str | None:
    if not _table_exists(con, "vocab", "_load_log"):
        return None
    row = con.execute("SELECT max(loaded_at) FROM vocab._load_log").fetchone()
    return row[0].isoformat() if row and row[0] else None


@dataclass(frozen=True)
class SourceRow:
    endpoint_id: str
    outcome_type: str
    measure_raw: str | None
    description_raw: str | None
    time_frame_raw: str | None
    population: str | None
    conformed: bool
    form_id: str | None = None
    measurement_id: str | None = None
    reference_id: str | None = None
    event_id: str | None = None
    scale_id: str | None = None
    direction_id: str | None = None
    timepoint_pattern: str | None = None
    timepoint_extracted: str | None = None
    threshold_comparator: str | None = None
    threshold_value: float | None = None
    threshold_unit: str | None = None
    form_match_method: str | None = None
    measurement_match_method: str | None = None
    reference_match_method: str | None = None
    event_match_method: str | None = None
    named_endpoint_id: str | None = None
    analysable: bool | None = None
    review_reason: str | None = None
    form_confidence: float | None = None
    measurement_confidence: float | None = None
    reference_confidence: float | None = None
    event_confidence: float | None = None

    @property
    def level(self) -> str:
        return codes.level_for_outcome_type(self.outcome_type)


_CONFORMED_SELECT = """
SELECT endpoint_id, outcome_type, measure_raw, description_raw, time_frame_raw, population,
       form_id, measurement_id, reference_id, scale_id, direction_id,
       timepoint_pattern, timepoint_extracted,
       threshold_comparator, threshold_value, threshold_unit,
       form_match_method, measurement_match_method, reference_match_method, analysable,
       event_id, event_match_method, named_endpoint_id,
       form_confidence, measurement_confidence, reference_confidence, event_confidence
FROM conformed.endpoints WHERE nct_id = ?
"""

_REVIEW_SELECT = """
SELECT review_id, outcome_type, measure_raw, description_raw, time_frame_raw, population, reason
FROM conformed.review_queue WHERE nct_id = ?
"""


def fetch_rows(con: duckdb.DuckDBPyConnection, nct_id: str) -> list[SourceRow]:
    """Every design_outcome for this trial, from both conformed tables. The
    two share `conform`'s content hash, so the union has no duplicates."""
    if not _table_exists(con, "raw", "studies"):
        raise NotPulled(f"{nct_id} is not in the warehouse -- run `endpoints pull` first")
    if not con.execute("SELECT 1 FROM raw.studies WHERE nct_id = ?", [nct_id]).fetchone():
        raise NotPulled(f"{nct_id} has not been pulled into raw.studies")
    if not _table_exists(con, "conformed", "endpoints"):
        raise NotConformed("conformed.endpoints is empty -- run `endpoints conform` first")

    rows: list[SourceRow] = []
    for r in con.execute(_CONFORMED_SELECT, [nct_id]).fetchall():
        rows.append(
            SourceRow(
                endpoint_id=r[0], outcome_type=r[1], measure_raw=r[2], description_raw=r[3],
                time_frame_raw=r[4], population=r[5], conformed=True,
                form_id=r[6], measurement_id=r[7], reference_id=r[8], scale_id=r[9],
                direction_id=r[10], timepoint_pattern=r[11], timepoint_extracted=r[12],
                threshold_comparator=r[13], threshold_value=r[14], threshold_unit=r[15],
                form_match_method=r[16], measurement_match_method=r[17],
                reference_match_method=r[18], analysable=r[19],
                event_id=r[20], event_match_method=r[21], named_endpoint_id=r[22],
                form_confidence=r[23], measurement_confidence=r[24],
                reference_confidence=r[25], event_confidence=r[26],
            )
        )
    if _table_exists(con, "conformed", "review_queue"):
        for r in con.execute(_REVIEW_SELECT, [nct_id]).fetchall():
            rows.append(
                SourceRow(
                    endpoint_id=r[0], outcome_type=r[1], measure_raw=r[2], description_raw=r[3],
                    time_frame_raw=r[4], population=r[5], conformed=False, review_reason=r[6],
                )
            )

    if not rows and _raw_outcome_count(con, nct_id):
        raise NotConformed(
            f"{nct_id} has raw.design_outcomes rows but none are conformed -- "
            "run `endpoints conform`"
        )
    rows.sort(key=lambda row: (codes.level_rank(row.level), row.endpoint_id))
    return rows


def _raw_outcome_count(con: duckdb.DuckDBPyConnection, nct_id: str) -> int:
    if not _table_exists(con, "raw", "design_outcomes"):
        return 0
    return con.execute(
        "SELECT count(*) FROM raw.design_outcomes WHERE nct_id = ?", [nct_id]
    ).fetchone()[0]


@dataclass
class Projection:
    nct_id: str
    study_id: str
    objectives: list[dict] = field(default_factory=list)
    dictionaries: list[dict] = field(default_factory=list)
    bc_surrogates: list[dict] = field(default_factory=list)
    analysis_populations: list[dict] = field(default_factory=list)
    tiers: dict[str, int] = field(default_factory=dict)
    # Per-tag count of endpoints whose host carries an announced default,
    # e.g. {"reference": 3}. A subset of `tiers["templated"]`.
    defaulted: dict[str, int] = field(default_factory=dict)
    endpoint_count: int = 0

    def endpoints(self) -> list[dict]:
        return [e for o in self.objectives for e in o["endpoints"]]


def _code(ids: IdFactory, code: str, decode: str) -> dict:
    return {
        "id": ids.mint("Code"),
        "extensionAttributes": [],
        "code": code,
        "codeSystem": codes.CODE_SYSTEM,
        "codeSystemVersion": codes.CODE_SYSTEM_VERSION,
        "decode": decode,
        "instanceType": "Code",
    }


def _extension(ids: IdFactory, url: str, **value: Any) -> dict:
    attribute = {
        "id": ids.mint("ExtensionAttribute"),
        "url": f"{codes.EXTENSION_NS}:{url}",
        "instanceType": "ExtensionAttribute",
    }
    attribute.update(value)
    return attribute


def _humanise(term_id: str) -> str:
    return term_id.replace("_", " ")


def _resolve_tags(
    row: SourceRow, spec: TemplateSpec, rules: ProjectionRules
) -> tuple[dict[str, str | None], bool]:
    """Every tag's rendered value (None where unresolved), and whether
    `reference` came from `spec.reference_fallback`."""
    measurement = rules.inline_label("measurement", row.measurement_id)
    concept_id = rules.measurement_concept.get(row.measurement_id or "")
    reference = rules.inline_label("reference", row.reference_id)
    reference_defaulted = False
    if reference is None and spec.reference_fallback:
        reference = rules.inline_label("reference", spec.reference_fallback)
        reference_defaulted = reference is not None
    values = {
        "measurement": measurement,
        "concept": _humanise(concept_id) if concept_id else None,
        "reference": reference,
        "event": rules.inline_label("event", row.event_id),
        "scale": rules.inline_label("scale", row.scale_id),
        "timepoint": render_timepoint(row.timepoint_pattern, row.timepoint_extracted, row.time_frame_raw),
        "threshold": render_threshold(row.threshold_comparator, row.threshold_value, row.threshold_unit),
    }
    return values, reference_defaulted


def _verbatim_text(row: SourceRow) -> str:
    for candidate in (row.measure_raw, row.description_raw):
        text = " ".join((candidate or "").split())
        if text:
            return text
    return "Endpoint not stated in the source registry record."


class _SharedInstances:
    """BiomedicalConceptSurrogates and AnalysisPopulations, minted once per trial."""

    def __init__(self, ids: IdFactory, rules: ProjectionRules) -> None:
        self._ids = ids
        self._rules = rules
        self.surrogates: dict[str, dict] = {}
        self.populations: dict[str, dict] = {}

    def surrogate(self, key: str, name: str, label: str, dimension: str) -> dict:
        if key not in self.surrogates:
            self.surrogates[key] = {
                "id": self._ids.mint("BiomedicalConceptSurrogate"),
                "extensionAttributes": [],
                "name": name,
                "label": label,
                "description": self._rules.definitions.get(dimension, {}).get(name, ""),
                "reference": f"{codes.VOCAB_REFERENCE_BASE}/{dimension}/{name}",
                "notes": [],
                "instanceType": "BiomedicalConceptSurrogate",
            }
        return self.surrogates[key]

    def population(self, text: str) -> dict:
        if text not in self.populations:
            self.populations[text] = {
                "id": self._ids.mint("AnalysisPopulation"),
                "extensionAttributes": [],
                "name": f"POP{len(self.populations) + 1}",
                "label": text,
                "description": "",
                "text": text,
                "subsetOfIds": [],
                "notes": [],
                "instanceType": "AnalysisPopulation",
            }
        return self.populations[text]


def _timepoint_extracted_fields(extracted: Any) -> list[tuple[str, str]]:
    """`timepoint_extracted`'s fields as `timepoint<Field>` pairs, sorted by key."""
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except (TypeError, ValueError):
            extracted = None
    if not extracted:
        return []
    fields: list[tuple[str, str]] = []
    for key in sorted(extracted):
        value = extracted[key]
        if value is None or value == "":
            continue
        camel = "".join(part.capitalize() for part in key.split("_"))
        fields.append((f"timepoint{camel}", str(value)))
    return fields


def _decomposition(
    ids: IdFactory, row: SourceRow, rules: ProjectionRules, analysis_population_id: str | None = None
) -> dict:
    """What the endpoint means: the resolved vocabulary terms, as one nested
    ExtensionAttribute. How they were decided lives in `_conformance`."""
    inner: list[dict] = []

    def add(url: str, value: Any) -> None:
        if value is None or str(value) == "":
            return
        inner.append(_extension(ids, url, valueString=str(value)))

    add("form", row.form_id)
    add("event", row.event_id)
    add("measurement", row.measurement_id)
    add("reference", row.reference_id)
    add("direction", row.direction_id)
    add("scale", row.scale_id)
    add("namedEndpoint", row.named_endpoint_id)
    add("thresholdComparator", row.threshold_comparator)
    add("thresholdUnit", row.threshold_unit)
    add("thresholdValue", row.threshold_value)
    add("timepointPattern", row.timepoint_pattern)
    add("timepointRole", rules.timepoint_role.get(row.timepoint_pattern or ""))
    add("timepointRaw", row.time_frame_raw)
    for url, value in _timepoint_extracted_fields(row.timepoint_extracted):
        add(url, value)
    if row.analysable is not None:
        inner.append(_extension(ids, "analysable", valueBoolean=bool(row.analysable)))
    add("analysisPopulationId", analysis_population_id)

    return {
        "id": ids.mint("ExtensionAttribute"),
        "url": f"{codes.EXTENSION_NS}:decomposition",
        "instanceType": "ExtensionAttribute",
        "valueExtensionClass": {
            "id": ids.mint("ExtensionClass"),
            "url": f"{codes.EXTENSION_NS}:decomposition",
            "extensionAttributes": inner,
            "instanceType": "ExtensionClass",
        },
    }


def _conformance(ids: IdFactory, row: SourceRow, tier: str) -> dict:
    """How the decomposition was decided: match methods, confidences, tier."""
    inner: list[dict] = []

    def add(url: str, value: Any) -> None:
        if value is None or str(value) == "":
            return
        inner.append(_extension(ids, url, valueString=str(value)))

    add("formMatchMethod", row.form_match_method)
    add("formMatchConfidence", row.form_confidence)
    add("measurementMatchMethod", row.measurement_match_method)
    add("measurementMatchConfidence", row.measurement_confidence)
    add("referenceMatchMethod", row.reference_match_method)
    add("referenceMatchConfidence", row.reference_confidence)
    add("eventMatchMethod", row.event_match_method)
    add("eventMatchConfidence", row.event_confidence)
    add("fidelity", tier)
    add("reviewReason", row.review_reason)
    add("sourceRowId", row.endpoint_id)

    return {
        "id": ids.mint("ExtensionAttribute"),
        "url": f"{codes.EXTENSION_NS}:conformance",
        "instanceType": "ExtensionAttribute",
        "valueExtensionClass": {
            "id": ids.mint("ExtensionClass"),
            "url": f"{codes.EXTENSION_NS}:conformance",
            "extensionAttributes": inner,
            "instanceType": "ExtensionClass",
        },
    }


@dataclass(frozen=True)
class _RenderResult:
    text: str
    tier: str
    rendered: Rendered | None
    values: dict[str, str | None]
    reference_defaulted: bool


def _render(row: SourceRow, rules: ProjectionRules) -> _RenderResult:
    spec = rules.templates.get(row.form_id or "")
    values, reference_defaulted = _resolve_tags(row, spec, rules) if spec else ({}, False)
    rendered = None
    degraded = False
    if row.conformed and spec and not spec.verbatim and spec.parts:
        rendered = render(spec.parts, values)
        if rendered is None and rules.event_family.get(row.form_id or ""):
            # An event-family row whose event did not resolve degrades to the
            # not_stated frame rather than to verbatim registry text.
            fallback_spec = rules.templates.get("not_stated")
            if fallback_spec and fallback_spec.parts:
                rendered = render(fallback_spec.parts, values)
                degraded = rendered is not None

    if rendered is None:
        tier = TIER_VERBATIM
    elif degraded or rendered.dropped or row.form_id == "not_stated":
        tier = TIER_PARTIAL
    else:
        tier = TIER_TEMPLATED

    text = rendered.text if rendered else f"<p>{escape(_verbatim_text(row), quote=False)}</p>"
    return _RenderResult(
        text=text, tier=tier, rendered=rendered, values=values, reference_defaulted=reference_defaulted
    )


def render_endpoint_text(row: SourceRow, rules: ProjectionRules) -> str:
    """`Endpoint.text` for one row; `conform` stores this as `usdm_text`."""
    return _render(row, rules).text


def _build_endpoint(
    row: SourceRow,
    rules: ProjectionRules,
    ids: IdFactory,
    shared: _SharedInstances,
    ordinal: int,
) -> tuple[dict, dict | None, str, frozenset[str]]:
    """One Endpoint, its dictionary (None at verbatim tier), its tier, and the
    tags whose host carries an announced default."""
    result = _render(row, rules)
    rendered, tier, values = result.rendered, result.tier, result.values

    # Flagged only once the default reached the document.
    defaulted: frozenset[str] = (
        frozenset({"reference"})
        if result.reference_defaulted and rendered is not None and "reference" in rendered.tags
        else frozenset()
    )

    name = f"END{ordinal}"
    endpoint_id = ids.mint("Endpoint")

    extensions: list[dict] = []
    parameter_maps: list[dict] = []
    dictionary: dict | None = None

    if rendered is not None:
        dictionary_id = ids.mint("SyntaxTemplateDictionary")
        for tag in dict.fromkeys(rendered.tags):
            host = TAG_HOSTS[tag]
            klass, attribute = HOST_REFERENCE_ATTRIBUTE[host]
            if host == "surrogate":
                if tag == "measurement":
                    dimension, term = "measurement", row.measurement_id
                elif tag == "event":
                    dimension, term = "event", row.event_id
                else:  # concept
                    dimension, term = "concept", str(values["concept"]).replace(" ", "_")
                instance = shared.surrogate(f"{dimension}:{term}", term, str(values[tag]), dimension)
                target_id = instance["id"]
            else:
                attribute_instance = _extension(ids, f"tag:{tag}", valueString=str(values[tag]))
                extensions.append(attribute_instance)
                target_id = attribute_instance["id"]
            parameter_maps.append(
                {
                    "id": ids.mint("ParameterMap"),
                    "extensionAttributes": [],
                    "tag": tag,
                    "reference": usdm_ref(klass, target_id, attribute),
                    "instanceType": "ParameterMap",
                }
            )
        dictionary = {
            "id": dictionary_id,
            "extensionAttributes": [],
            "name": f"{name}_Dict",
            "label": "",
            "description": "",
            "parameterMaps": parameter_maps,
            "instanceType": "SyntaxTemplateDictionary",
        }

    # The measurement surrogate is minted for every conformed endpoint,
    # tag-referenced or not, so event-family rows keep the cross-study join.
    measurement_label = values.get("measurement")
    if row.conformed and row.measurement_id not in UNRESOLVED_TERM_IDS and measurement_label:
        shared.surrogate(f"measurement:{row.measurement_id}", row.measurement_id, measurement_label, "measurement")

    population = (row.population or "").strip()
    population_id = shared.population(population)["id"] if population else None
    extensions.append(_decomposition(ids, row, rules, population_id))
    extensions.append(_conformance(ids, row, tier))
    extensions.append(_extension(ids, "derived", valueString="purpose"))
    if "reference" in defaulted:
        extensions.append(_extension(ids, "derived", valueString="reference"))

    domain = rules.measurement_domain.get(row.measurement_id or "")
    purpose = rules.purposes.get(domain) or rules.purposes.get("_default", "")

    endpoint = {
        "id": endpoint_id,
        "extensionAttributes": extensions,
        "name": name,
        "label": rendered.label if rendered else _verbatim_text(row),
        "description": " ".join((row.measure_raw or "").split()),
        "text": result.text,
        "dictionaryId": dictionary["id"] if dictionary else None,
        "notes": [],
        "purpose": purpose,
        "level": _code(ids, *codes.ENDPOINT_LEVEL_CODES[row.level]),
        "instanceType": "Endpoint",
    }
    return endpoint, dictionary, tier, defaulted


def _endpoint_concept(row: SourceRow, rules: ProjectionRules) -> str:
    """An event-family endpoint's objective is about the event, not the
    assessment; otherwise the measurement's concept."""
    if rules.event_family.get(row.form_id or "") and row.event_id not in UNRESOLVED_TERM_IDS:
        concept = rules.event_concept.get(row.event_id or "")
        if concept:
            return concept
    return rules.measurement_concept.get(row.measurement_id or "", "")


def _concept_list(concepts: Iterable[str], limit: int) -> str | None:
    unique = list(dict.fromkeys(c for c in concepts if c))
    if not unique:
        return None
    shown = unique[:limit]
    phrase = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    return f"{phrase} and others" if len(unique) > limit else phrase


def project(
    con: duckdb.DuckDBPyConnection,
    nct_id: str,
    *,
    rules: ProjectionRules | None = None,
    levels: Iterable[str] | None = None,
    tiers: Iterable[str] | None = None,
) -> Projection:
    """Project one trial's endpoints. Deterministic for a given warehouse state."""
    rules = rules or load_projection_rules(con)
    rows = fetch_rows(con, nct_id)
    wanted_levels = set(levels) if levels else None
    wanted_tiers = set(tiers) if tiers else None

    ids = IdFactory()
    study_id = ids.mint("Study")
    shared = _SharedInstances(ids, rules)

    built: list[tuple[SourceRow, dict, dict | None, str, frozenset[str]]] = []
    for ordinal, row in enumerate(rows, start=1):
        if wanted_levels and row.level not in wanted_levels:
            continue
        endpoint, dictionary, tier, defaulted = _build_endpoint(row, rules, ids, shared, ordinal)
        if wanted_tiers and tier not in wanted_tiers:
            continue
        built.append((row, endpoint, dictionary, tier, defaulted))

    projection = Projection(nct_id=nct_id, study_id=study_id)
    projection.endpoint_count = len(built)
    for _row, _endpoint, _dictionary, tier, defaulted in built:
        projection.tiers[tier] = projection.tiers.get(tier, 0) + 1
        for tag in defaulted:
            projection.defaulted[tag] = projection.defaulted.get(tag, 0) + 1
    projection.dictionaries = [d for _r, _e, d, _t, _def in built if d]

    for level in codes.LEVEL_ORDER:
        at_level = [(r, e) for r, e, _d, _t, _def in built if r.level == level]
        if not at_level:
            continue
        index = len(projection.objectives) + 1
        concepts = [_humanise(_endpoint_concept(r, rules)) for r, _e in at_level]
        concept_list = _concept_list(concepts, rules.concept_list_limit)
        template = rules.objective_templates.get(level, "")
        if concept_list and template:
            # Plain prose with no dictionary: {concept_list} names several
            # concepts, and a ParameterMap references exactly one instance.
            label = render(parse_template(template), {"concept_list": concept_list}).label
        else:
            label = rules.objective_templates.get("_unresolved", "")
        text = f"<p>{escape(label, quote=False)}</p>"
        projection.objectives.append(
            {
                "id": ids.mint("Objective"),
                "extensionAttributes": [_extension(ids, "derived", valueString="objective")],
                "name": f"OBJ{index}",
                "label": label,
                "description": "",
                "text": text,
                "dictionaryId": None,
                "notes": [],
                "level": _code(ids, *codes.OBJECTIVE_LEVEL_CODES[level]),
                "endpoints": [e for _r, e in at_level],
                "instanceType": "Objective",
            }
        )

    projection.bc_surrogates = list(shared.surrogates.values())
    projection.analysis_populations = list(shared.populations.values())
    return projection
