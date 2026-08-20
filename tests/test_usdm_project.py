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
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
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


def test_provenance_carries_the_pull_it_came_from(usdm_con):
    """raw._pull_log.pulled_at is a TIMESTAMPTZ, which duckdb's Python client can
    only hand back as a datetime when pytz is importable -- and it does not
    depend on pytz. Reading it as text keeps the projection working on an
    install that hasn't got it."""
    body = module_envelope(usdm_con, project(usdm_con, "NCT00000001"), vocab_version="test")

    assert body["provenance"]["source"] == "ctgov_api"
    pulled_at = body["provenance"]["pulledAt"]
    assert pulled_at.endswith("+00:00")
    parsed = datetime.fromisoformat(pulled_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # Same shape datetime.isoformat() produced before it was rendered in SQL.
    assert parsed.isoformat() == pulled_at


# ------------------------------------------------- event semantics (Phase C)


@pytest.fixture(scope="module")
def nct01777919_con():
    """A standalone warehouse carrying exactly the two NCT01777919 outcomes
    the spec's incident and worked example are built from, isolated from the
    shared `usdm_con` fixture so this test pins its own expectations without
    perturbing every other test that relies on the shared one."""
    from clinical_endpoints.conform.pipeline import run_conform
    from clinical_endpoints.db import SCHEMAS
    from clinical_endpoints.ingest.design import DESIGN_GROUPS_DDL, STUDIES_DDL
    from clinical_endpoints.ingest.pull_log import write_pull_log
    from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, write_vocab_tables

    con = duckdb.connect(":memory:")
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    vocab_dir = default_vocab_dir(Path(__file__).parent)
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)

    con.execute(f"CREATE TABLE raw.studies ({STUDIES_DDL})")
    con.execute(
        "INSERT INTO raw.studies VALUES (" + ", ".join(["?"] * 19) + ")",
        [
            "NCT01777919", "PHASE3", "COMPLETED", "INTERVENTIONAL", "2013-01-01", "2016-01-01",
            "A trial of tumour-targeted therapy", "A trial of tumour-targeted therapy, officially",
            "Parallel Assignment", "Treatment", "Randomized", "Double", 480, "Actual",
            False, "All", "18 Years", "75 Years", "Adults with advanced solid tumours",
        ],
    )
    con.execute(f"CREATE TABLE raw.design_groups ({DESIGN_GROUPS_DDL})")
    con.execute(
        "CREATE TABLE raw.design_outcomes (nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR, "
        "time_frame VARCHAR, description VARCHAR, population VARCHAR)"
    )
    con.executemany(
        "INSERT INTO raw.design_outcomes VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("NCT01777919", "primary", "Progression-free survival", "6 months", None, None),
            ("NCT01777919", "secondary", "Overall survival", "2 years", None, None),
            # A third, synthetic row: event-family (time_to_event) but names no
            # recognisable event -- exercises the degraded not_stated frame.
            ("NCT01777919", "other", "Time to RECIST assessment", None, None, None),
        ],
    )
    write_pull_log(
        con, source="ctgov_api", filters={}, row_counts={"studies": 1, "design_outcomes": 3},
        source_tables=("studies", "design_outcomes", "design_groups"),
    )
    run_conform(con)
    yield con
    con.close()


def test_nct01777919_pfs_row_projects_the_worked_example(nct01777919_con):
    """The regression test the incident earns, at the projection layer: PFS no
    longer renders 'Time from randomisation to Tumour burden (RECIST)' -- the
    wrong-endpoint-definition defect this whole spec exists to fix."""
    projection = project(nct01777919_con, "NCT01777919", rules=load_projection_rules(nct01777919_con))
    pfs = next(e for e in projection.endpoints() if e["description"] == "Progression-free survival")

    assert pfs["label"] == "Time from randomisation to disease progression or death over 6 months"
    assert pfs["text"] == (
        '<p>Time from <usdm:tag name="reference"/> to <usdm:tag name="event"/> '
        '<usdm:tag name="timepoint"/></p>'
    )
    deco = {
        ext["url"].rsplit(":", 1)[-1]: ext.get("valueString")
        for ext in next(
            e for e in pfs["extensionAttributes"] if e["url"].endswith(":decomposition")
        )["valueExtensionClass"]["extensionAttributes"]
    }
    assert deco["reference"] == "randomisation"
    assert deco["event"] == "disease_progression_or_death"
    assert deco["measurement"] == "tumour_burden_recist"
    assert deco["namedEndpoint"] == "pfs"
    assert deco["eventMatchMethod"] == "named_endpoint"
    assert deco["fidelity"] == "templated"

    # The measurement surrogate still carries the ORR join, even though the
    # template renders {event} rather than {measurement}.
    surrogates_by_name = {s["name"]: s for s in projection.bc_surrogates}
    assert "tumour_burden_recist" in surrogates_by_name
    assert surrogates_by_name["tumour_burden_recist"]["reference"] == "/v4/vocab/measurement/tumour_burden_recist"
    assert "disease_progression_or_death" in surrogates_by_name
    assert surrogates_by_name["disease_progression_or_death"]["reference"] == "/v4/vocab/event/disease_progression_or_death"

    dictionary = next(d for d in projection.dictionaries if d["id"] == pfs["dictionaryId"])
    tags_used = {pm["tag"] for pm in dictionary["parameterMaps"]}
    assert tags_used == {"reference", "event", "timepoint"}
    assert "measurement" not in tags_used  # present in bcSurrogates, not referenced by this dictionary


def test_nct01777919_os_row_projects_the_worked_example(nct01777919_con):
    projection = project(nct01777919_con, "NCT01777919", rules=load_projection_rules(nct01777919_con))
    os_endpoint = next(e for e in projection.endpoints() if e["description"] == "Overall survival")
    assert os_endpoint["label"] == "Time from randomisation to death from any cause over 2 years"


def test_nct01777919_primary_objective_is_about_progression_not_tumour_burden(nct01777919_con):
    """"To evaluate the effect of the study intervention on tumour burden" was
    the defect (point 4 in the spec's "defect, shown on the first live trial"
    section) -- the objective must now name the event's concept."""
    projection = project(nct01777919_con, "NCT01777919", rules=load_projection_rules(nct01777919_con))
    primary = next(o for o in projection.objectives if o["level"]["decode"] == "Primary Objective")
    assert primary["label"] == "To evaluate the effect of the study intervention on disease progression"


def test_event_family_row_with_unresolvable_event_degrades_to_the_not_stated_frame(nct01777919_con):
    """An event-family row whose event does not resolve must not render the
    assessment into the event's slot -- it degrades to {measurement}[
    {timepoint}] at partial tier instead of falling to raw verbatim text."""
    projection = project(nct01777919_con, "NCT01777919", rules=load_projection_rules(nct01777919_con))
    degraded = next(e for e in projection.endpoints() if e["description"] == "Time to RECIST assessment")

    assert degraded["label"] == "Tumour burden (RECIST)"
    assert degraded["text"] == '<p><usdm:tag name="measurement"/></p>'
    deco = {
        ext["url"].rsplit(":", 1)[-1]: ext.get("valueString")
        for ext in next(
            e for e in degraded["extensionAttributes"] if e["url"].endswith(":decomposition")
        )["valueExtensionClass"]["extensionAttributes"]
    }
    assert deco["fidelity"] == "partial"
    assert deco["event"] == "not_stated"
    assert deco["measurement"] == "tumour_burden_recist"


def test_provenance_survives_a_warehouse_with_no_pull_log(usdm_warehouse_path, tmp_path):
    """A warehouse built by hand, or one whose log predates the table."""
    import shutil

    copy = tmp_path / "no_log.duckdb"
    shutil.copy(usdm_warehouse_path, copy)
    con = duckdb.connect(str(copy))
    try:
        con.execute("DROP TABLE raw._pull_log")
        body = module_envelope(con, project(con, "NCT00000001"), vocab_version="test")
    finally:
        con.close()

    assert body["provenance"]["source"] is None
    assert body["provenance"]["pulledAt"] is None
