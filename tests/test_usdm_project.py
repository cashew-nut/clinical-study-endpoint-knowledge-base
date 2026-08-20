"""The USDM 4.0 projection: fidelity tiers, tag/parameterMap bijection, row
conservation, determinism, and conformance to the published USDM schema.

The vendored schema in `src/clinical_endpoints/usdm/schema/` is
`components.schemas` from DDF-RA `Deliverables/API/USDM_API.yaml` at tag
v4.0.0, with `$ref` targets rewritten to `#/$defs/`. Regenerate with:

    doc = yaml.safe_load(open("USDM_API.yaml"))
    bundle = json.loads(
        json.dumps({"$defs": doc["components"]["schemas"]})
        .replace("#/components/schemas/", "#/$defs/")
    )
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clinical_endpoints.usdm import codes
from clinical_endpoints.usdm.codes import UnknownOutcomeType, level_for_outcome_type
from clinical_endpoints.usdm.envelope import module_envelope, wrapper_envelope
from clinical_endpoints.usdm.project import (
    NotConformed,
    NotPulled,
    load_projection_rules,
    project,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/clinical_endpoints/usdm/schema/usdm_4_0_0.json"
)
_BUNDLE = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

TAG_RE = re.compile(r'<usdm:tag name="([a-z0-9_]+)"/>')


def schema_for(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": _BUNDLE["$defs"]})


@pytest.fixture
def projection(usdm_con):
    return project(usdm_con, "NCT00000001", rules=load_projection_rules(usdm_con))


# ----------------------------------------------------------- the core invariant


def test_every_raw_outcome_row_becomes_exactly_one_endpoint(usdm_con):
    """Never fewer. A trial whose endpoints did not conform is a trial whose
    endpoints render less richly -- not one that appears to have fewer."""
    rules = load_projection_rules(usdm_con)
    for (nct_id, raw_count) in usdm_con.execute(
        "SELECT nct_id, count(*) FROM raw.design_outcomes GROUP BY nct_id ORDER BY nct_id"
    ).fetchall():
        assert project(usdm_con, nct_id, rules=rules).endpoint_count == raw_count


def test_review_queue_rows_are_projected_verbatim_not_dropped(projection):
    verbatim = [e for e in projection.endpoints() if e["dictionaryId"] is None]
    assert [e["label"] for e in verbatim] == ["Sponsor internal reference code"]
    assert projection.tiers["verbatim"] == 1
    # The raw string survives in `text` as well, so nothing is lost.
    assert verbatim[0]["text"] == "<p>Sponsor internal reference code</p>"


def test_tiers_split_templated_partial_and_verbatim(projection):
    assert projection.tiers == {"templated": 3, "partial": 1, "verbatim": 1}


# ------------------------------------------------------------------- the tags


def test_every_tag_in_text_has_exactly_one_parameter_map_and_vice_versa(usdm_con):
    """The property that catches most renderer bugs: a document where the two
    disagree is malformed, whatever else is right about it."""
    rules = load_projection_rules(usdm_con)
    for nct_id in ("NCT00000001", "NCT00000002"):
        p = project(usdm_con, nct_id, rules=rules)
        by_id = {d["id"]: d for d in p.dictionaries}
        for endpoint in p.endpoints():
            in_text = TAG_RE.findall(endpoint["text"])
            if endpoint["dictionaryId"] is None:
                assert in_text == []
                continue
            mapped = [pm["tag"] for pm in by_id[endpoint["dictionaryId"]]["parameterMaps"]]
            assert sorted(in_text) == sorted(mapped), endpoint["name"]
            assert len(mapped) == len(set(mapped))


def test_rendered_label_has_no_residual_markup(usdm_con):
    rules = load_projection_rules(usdm_con)
    for nct_id in ("NCT00000001", "NCT00000002"):
        for endpoint in project(usdm_con, nct_id, rules=rules).endpoints():
            assert "<" not in endpoint["label"] and "{" not in endpoint["label"]


def test_every_reference_resolves_to_an_instance_in_the_document(projection):
    """`ParameterMap.reference` is a usdm:ref naming klass/id/attribute -- a
    reference to an id that is nowhere in the document is a dangling pointer."""
    known = {s["id"] for s in projection.bc_surrogates}
    known |= {a["id"] for a in projection.analysis_populations}
    for endpoint in projection.endpoints():
        known |= {ext["id"] for ext in endpoint["extensionAttributes"]}

    for dictionary in projection.dictionaries:
        for pm in dictionary["parameterMaps"]:
            match = re.search(r'id="([^"]+)"', pm["reference"])
            assert match, pm["reference"]
            assert match.group(1) in known, pm


def test_a_measurement_surrogate_is_shared_and_carries_its_vocabulary_reference(usdm_con):
    rules = load_projection_rules(usdm_con)
    p = project(usdm_con, "NCT00000002", rules=rules)
    names = [s["name"] for s in p.bc_surrogates]
    assert len(names) == len(set(names)), "one surrogate per distinct term"
    for surrogate in p.bc_surrogates:
        assert surrogate["reference"].startswith(codes.VOCAB_REFERENCE_BASE)


# ------------------------------------------------------------------ the levels


@pytest.mark.parametrize(
    "outcome_type, level",
    [
        ("primary", "primary"), ("Primary", "primary"),
        ("secondary", "secondary"), ("Secondary", "secondary"),
        ("other", "exploratory"), ("Other Pre-specified", "exploratory"),
        ("Post-Hoc", "exploratory"), ("OTHER_PRE_SPECIFIED", "exploratory"),
    ],
)
def test_both_backends_outcome_type_vocabularies_map_to_a_level(outcome_type, level):
    """ctgov_api writes lowercase primary/secondary/other; aact passes AACT's
    title-case values through untouched. Both have to land."""
    assert level_for_outcome_type(outcome_type) == level


def test_an_unknown_outcome_type_raises_rather_than_defaulting():
    """Defaulting would mislabel a primary endpoint, which is the one thing a
    consumer of this API most relies on."""
    with pytest.raises(UnknownOutcomeType):
        level_for_outcome_type("tertiary")


def test_endpoint_and_objective_levels_use_the_published_ct_codes(projection):
    by_level = {o["level"]["decode"]: o for o in projection.objectives}
    assert set(by_level) == {"Primary Objective", "Secondary Objective", "Exploratory Objective"}
    assert by_level["Primary Objective"]["level"]["code"] == "C85826"
    primary_endpoints = by_level["Primary Objective"]["endpoints"]
    assert primary_endpoints[0]["level"]["code"] == "C94496"
    assert primary_endpoints[0]["level"]["codeSystem"] == "http://www.cdisc.org"


# ------------------------------------------------------------- determinism etc


def test_two_projections_of_unchanged_state_are_byte_identical(usdm_con):
    rules = load_projection_rules(usdm_con)
    first = module_envelope(usdm_con, project(usdm_con, "NCT00000001", rules=rules))
    second = module_envelope(usdm_con, project(usdm_con, "NCT00000001", rules=rules))
    first["provenance"].pop("projectedAt")
    second["provenance"].pop("projectedAt")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_filters_narrow_by_level_and_by_tier(usdm_con):
    rules = load_projection_rules(usdm_con)
    primary = project(usdm_con, "NCT00000001", rules=rules, levels=["primary"])
    assert primary.endpoint_count == 1
    assert [o["level"]["decode"] for o in primary.objectives] == ["Primary Objective"]

    templated = project(usdm_con, "NCT00000001", rules=rules, tiers=["templated"])
    assert set(templated.tiers) == {"templated"}


def test_unknown_and_unconformed_trials_are_distinguishable(usdm_con):
    rules = load_projection_rules(usdm_con)
    with pytest.raises(NotPulled):
        project(usdm_con, "NCT09999999", rules=rules)
    # Pulled, registered no outcomes: an empty projection, not an error.
    assert project(usdm_con, "NCT00000003", rules=rules).endpoint_count == 0


def test_purpose_is_derived_from_the_measurement_domain_and_flagged(projection):
    endpoints = projection.endpoints()
    assert endpoints[0]["purpose"] == "To assess the efficacy of the study intervention."
    flags = [
        ext["valueString"]
        for ext in endpoints[0]["extensionAttributes"]
        if ext["url"].endswith(":derived")
    ]
    assert flags == ["purpose"]


def test_objectives_are_synthesized_and_say_so(projection):
    for objective in projection.objectives:
        assert any(
            ext["url"].endswith(":derived") and ext["valueString"] == "objective"
            for ext in objective["extensionAttributes"]
        )
    # A concept list cannot be one reference, so an objective carries no dictionary.
    assert all(o["dictionaryId"] is None for o in projection.objectives)
    assert "<usdm:tag" not in projection.objectives[0]["text"]


# ------------------------------------------------------------ schema conformance


@pytest.mark.parametrize(
    "member, schema_name",
    [
        ("objectives", "Objective-Output"),
        ("dictionaries", "SyntaxTemplateDictionary-Output"),
        ("bcSurrogates", "BiomedicalConceptSurrogate-Output"),
        ("analysisPopulations", "AnalysisPopulation-Output"),
    ],
)
def test_module_members_validate_against_the_published_usdm_schema(usdm_con, member, schema_name):
    body = module_envelope(usdm_con, project(usdm_con, "NCT00000001"))
    validator = schema_for(schema_name)
    assert body[member], f"{member} is empty -- the assertion would be vacuous"
    for instance in body[member]:
        assert list(validator.iter_errors(instance)) == []


def test_the_wrapper_envelope_validates_as_a_usdm_wrapper(usdm_con):
    rules = load_projection_rules(usdm_con)
    for nct_id in ("NCT00000001", "NCT00000002"):
        body = wrapper_envelope(usdm_con, project(usdm_con, nct_id, rules=rules))
        payload = {k: v for k, v in body.items() if k != "provenance"}
        assert list(schema_for("Wrapper-Output").iter_errors(payload)) == []


def test_the_wrapper_names_every_placeholder_it_had_to_invent(usdm_con):
    """A placeholder that is not announced is a fabricated clinical fact."""
    body = wrapper_envelope(usdm_con, project(usdm_con, "NCT00000002"))
    synthesized = " ".join(body["provenance"]["synthesized"])
    assert "includesHealthySubjects" in synthesized
    assert "InterventionalStudyDesign.model" in synthesized
    notes = body["study"]["versions"][0]["notes"][0]["text"]
    assert "includesHealthySubjects" in notes


def test_the_wrapper_uses_sourced_design_facts_when_the_pull_has_them(usdm_con):
    body = wrapper_envelope(usdm_con, project(usdm_con, "NCT00000001"))
    design = body["study"]["versions"][0]["studyDesigns"][0]
    assert design["model"]["decode"] == "Parallel Assignment"
    assert design["population"]["includesHealthySubjects"] is False
    assert design["population"]["plannedEnrollmentNumber"]["value"] == 480.0
    assert [arm["name"] for arm in design["arms"]] == ["Drug A", "Placebo"]
    assert [c["decode"] for c in design["characteristics"]] == ["Randomized"]
    assert "includesHealthySubjects" not in " ".join(body["provenance"]["synthesized"])


def test_a_concept_tag_resolves_to_a_shared_surrogate(usdm_con):
    """`{concept}` is a legal tag no shipped template uses yet, so exercise it
    directly rather than leaving the path untested."""
    import dataclasses

    from clinical_endpoints.usdm.project import TemplateSpec
    from clinical_endpoints.usdm.templates import parse_template

    rules = load_projection_rules(usdm_con)
    template = "{measurement}, a measure of {concept}[ {timepoint}]"
    patched = dict(rules.templates)
    patched["change_from_baseline"] = TemplateSpec(
        form_id="change_from_baseline",
        template=template,
        verbatim=False,
        reference_fallback=None,
        parts=parse_template(template),
    )
    p = project(usdm_con, "NCT00000001", rules=dataclasses.replace(rules, templates=patched))

    endpoint = next(e for e in p.endpoints() if e["name"] == "END2")
    assert '<usdm:tag name="concept"/>' in endpoint["text"]
    assert endpoint["label"].startswith("Glycated haemoglobin (HbA1c), a measure of glycaemic control")

    by_name = {s["name"]: s for s in p.bc_surrogates}
    assert by_name["glycaemic_control"]["reference"].endswith("/concept/glycaemic_control")
    dictionary = next(d for d in p.dictionaries if d["id"] == endpoint["dictionaryId"])
    concept_map = next(pm for pm in dictionary["parameterMaps"] if pm["tag"] == "concept")
    assert by_name["glycaemic_control"]["id"] in concept_map["reference"]
