"""Tests for the vocab loader/validator, and for the vocabularies themselves.

Two kinds of test here, deliberately mixed:

* structural tests that the validator catches a broken vocab file (mutate a good
  vocabulary, assert the specific error), and
* content tests that the shipped vocabularies conform the endpoints from the
  implementation plan's reference table -- PFS, ORR, FEV1-CFB, HbA1c-CFB, ACR20.
  Those are the acceptance fixtures for build-order step 3, so failing them here
  means step 3 cannot pass either.
"""

from __future__ import annotations

import copy
import re

import duckdb
import pytest

from clinical_endpoints.vocab.loader import (
    VocabError,
    default_vocab_dir,
    load_vocab,
    normalise,
    validate_vocab,
    write_vocab_tables,
)
from clinical_endpoints.vocab.schema import DIMENSIONS


@pytest.fixture(scope="module")
def vocab_dir():
    return default_vocab_dir(__file__)


@pytest.fixture(scope="module")
def docs(vocab_dir):
    return load_vocab(vocab_dir)


# ------------------------------------------------------------------ loading


def test_shipped_vocabulary_validates_clean(docs):
    result = validate_vocab(docs)
    assert result.errors == [], "shipped vocabulary has validation errors"


def test_shipped_vocabulary_has_no_warnings(docs):
    result = validate_vocab(docs)
    assert result.warnings == [], "shipped vocabulary has validation warnings"


def test_every_dimension_file_is_present_and_declares_itself(docs):
    for spec in DIMENSIONS:
        assert docs[spec.dimension]["dimension"] == spec.dimension
        assert docs[spec.dimension]["terms"]


def test_missing_file_raises_vocab_error(tmp_path):
    with pytest.raises(VocabError, match="Missing vocab file"):
        load_vocab(tmp_path)


def test_unparseable_file_raises_vocab_error(tmp_path, vocab_dir):
    for spec in DIMENSIONS:
        (tmp_path / spec.filename).write_text((vocab_dir / spec.filename).read_text())
    (tmp_path / "ta_mesh_mapping.yaml").write_text("terms: [")
    with pytest.raises(VocabError, match="not valid YAML"):
        load_vocab(tmp_path)


# --------------------------------------------------------------- validation


def test_duplicate_term_id_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["form"]["terms"].append(dict(broken["form"]["terms"][0]))
    assert any("duplicate term id" in e for e in validate_vocab(broken).errors)


def test_synonym_claimed_by_two_terms_is_an_error(docs):
    broken = copy.deepcopy(docs)
    terms = broken["measurement"]["terms"]
    terms[1].setdefault("synonyms", []).append(terms[0]["synonyms"][0])
    assert any("claimed by both" in e for e in validate_vocab(broken).errors)


def test_uncompilable_regex_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["form"]["terms"][0].setdefault("patterns", []).append("([unclosed")
    assert any("does not compile" in e for e in validate_vocab(broken).errors)


def test_dangling_cross_file_reference_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["measurement"]["terms"][0]["default_scale"] = "furlongs"
    assert any("is not a scale id" in e for e in validate_vocab(broken).errors)


def test_precedence_list_out_of_step_with_terms_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["form"]["match_precedence"].remove("time_to_event")
    assert any("omits" in e and "time_to_event" in e for e in validate_vocab(broken).errors)


def test_unknown_direction_rule_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["form"]["terms"][0]["direction_rule"] = "vibes"
    assert any("direction_rule" in e for e in validate_vocab(broken).errors)


def test_undeclared_concept_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["measurement"]["terms"][0]["concept"] = "brand_new_concept"
    errors = validate_vocab(broken).errors
    assert any("used but not declared" in e for e in errors)


def test_measurement_must_not_auto_conform_when_unmatched(docs):
    broken = copy.deepcopy(docs)
    broken["measurement"]["on_unmatched"] = "not_stated"
    assert any("must never auto-conform" in e for e in validate_vocab(broken).errors)


def test_tied_therapeutic_area_precedence_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["therapeutic_area"]["terms"][1]["precedence"] = broken["therapeutic_area"]["terms"][0]["precedence"]
    assert any("must be unique" in e for e in validate_vocab(broken).errors)


def test_tied_timepoint_priority_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["timepoint_pattern"]["terms"][1]["priority"] = broken["timepoint_pattern"]["terms"][0]["priority"]
    assert any("must be unique" in e for e in validate_vocab(broken).errors)


def test_ta_mesh_mapping_to_unknown_area_is_an_error(docs):
    broken = copy.deepcopy(docs)
    broken["ta_mesh_mapping"]["term_overrides"]["Asthma"] = "pulmonology"
    assert any("unknown therapeutic area" in e for e in validate_vocab(broken).errors)


# -------------------------------------------------------------- persistence


def test_write_vocab_tables_is_idempotent(docs, vocab_dir):
    con = duckdb.connect(":memory:")
    first = write_vocab_tables(con, docs, vocab_dir=vocab_dir)
    second = write_vocab_tables(con, docs, vocab_dir=vocab_dir)
    # Term tables are replaced wholesale; only the load log accumulates.
    assert {k: v for k, v in first.items() if k != "_load_log"} == {
        k: v for k, v in second.items() if k != "_load_log"
    }
    assert con.execute("SELECT count(*) FROM vocab._load_log").fetchone()[0] == 2 * first["_load_log"]


def test_written_tables_are_queryable(docs, vocab_dir):
    con = duckdb.connect(":memory:")
    write_vocab_tables(con, docs, vocab_dir=vocab_dir)
    assert con.execute("SELECT count(*) FROM vocab.forms").fetchone()[0] == len(docs["form"]["terms"])
    assert con.execute(
        "SELECT direction_rule FROM vocab.forms WHERE id = 'responder_proportion'"
    ).fetchone()[0] == "higher_count_better"
    assert con.execute(
        "SELECT ta_id FROM vocab.ta_mesh_term_overrides WHERE mesh_term_normalised = ?",
        [normalise("Carcinoma, Non-Small-Cell Lung")],
    ).fetchone()[0] == "oncology"


def test_every_synonym_lands_in_the_synonyms_table(docs, vocab_dir):
    con = duckdb.connect(":memory:")
    write_vocab_tables(con, docs, vocab_dir=vocab_dir)
    expected = sum(
        len(term.get("synonyms") or [])
        for spec in DIMENSIONS
        for term in docs[spec.dimension]["terms"]
    )
    assert con.execute("SELECT count(*) FROM vocab.synonyms").fetchone()[0] == expected


# ----------------------------------------------- the vocabularies themselves


def _matcher(doc, order_key="match_precedence"):
    by_id = {t["id"]: t for t in doc["terms"]}
    order = doc.get(order_key) or list(by_id)
    compiled = []
    for term_id in order:
        term = by_id[term_id]
        compiled.append((
            term_id,
            [re.compile(p, re.I) for p in term.get("patterns") or []],
            [
                re.compile(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])")
                for s in term.get("synonyms") or []
            ],
        ))
    return compiled


def _match(compiled, text):
    for term_id, patterns, synonyms in compiled:
        if any(p.search(text) for p in patterns) or any(s.search(text) for s in synonyms):
            return term_id
    return None


def _derive_direction(docs, form_id, measurement_id, text):
    forms = {t["id"]: t for t in docs["form"]["terms"]}
    measurements = {t["id"]: t for t in docs["measurement"]["terms"]}
    form = forms[form_id or docs["form"]["default_when_unmatched"]]
    measurement = measurements.get(measurement_id, {})

    cues = docs["direction"]["event_polarity_cues"]
    polarity = measurement.get("event_polarity")
    for candidate in ("benefit", "harm"):
        if any(re.search(p, text, re.I) for p in cues[candidate]):
            polarity = candidate
            break

    rule = form["direction_rule"]
    if rule == "higher_count_better":
        return "increase_is_better"
    if rule == "neutral":
        return "neutral"
    if rule == "inherit_event_polarity":
        return {"harm": "decrease_is_better", "benefit": "increase_is_better"}.get(
            polarity, docs["direction"]["default_when_underivable"]
        )
    if rule == "time_polarity":
        return {"harm": "longer_is_better", "benefit": "shorter_is_better"}.get(
            polarity, docs["direction"]["default_when_underivable"]
        )
    return measurement.get("default_direction", docs["direction"]["default_when_underivable"])


# (measure text, expected form, expected measurement, expected direction)
REFERENCE_TABLE_FIXTURES = [
    ("Progression-Free Survival (PFS)", "time_to_event", "tumour_burden_recist", "longer_is_better"),
    ("Overall Survival (OS)", "time_to_event", "vital_status", "longer_is_better"),
    ("Objective Response Rate (ORR)", "responder_proportion", "tumour_burden_recist", "increase_is_better"),
    ("Change from Baseline in FEV1 at Week 12", "change_from_baseline", "fev1", "increase_is_better"),
    ("Change from baseline in Hemoglobin A1c (HbA1c)", "change_from_baseline", "hba1c", "decrease_is_better"),
    ("Proportion of participants achieving ACR20 response at Week 24",
     "responder_proportion", "acr_response_composite", "increase_is_better"),
    # Direction traps: same form, opposite answers.
    ("Number of Participants With Treatment Emergent Adverse Events (TEAEs)",
     "incidence_proportion", "adverse_event", "decrease_is_better"),
    ("30-day mortality", "incidence_proportion", "vital_status", "decrease_is_better"),
    ("2-Year Overall Survival (OS)", "event_free_rate_at_timepoint", "vital_status", "increase_is_better"),
    ("Time to Response (TTR)", "time_to_event", "tumour_burden_recist", "shorter_is_better"),
    ("Time to initiation of rescue medication over 40 weeks",
     "time_to_event", "rescue_medication_use", "longer_is_better"),
    # A bare instrument name conforms with form=not_stated, not as a failure.
    ("Psoriasis Area and Severity Index (PASI)", "not_stated", "pasi", "decrease_is_better"),
    ("Percent change in body weight from Baseline",
     "percent_change_from_baseline", "body_weight", "decrease_is_better"),
    ("Maximum Observed Plasma Concentration (Cmax) of Mitapivat",
     "value_at_timepoint", "pk_cmax", "neutral"),
    ("Safety and Tolerability", "descriptive", "adverse_event", "neutral"),
]


@pytest.mark.parametrize("text,form_id,measurement_id,direction_id", REFERENCE_TABLE_FIXTURES)
def test_reference_table_fixtures_conform(docs, text, form_id, measurement_id, direction_id):
    normalised = normalise(text)
    forms = _matcher(docs["form"])
    measurements = _matcher(docs["measurement"], order_key="_none")
    got_form = _match(forms, normalised) or docs["form"]["default_when_unmatched"]
    got_measurement = _match(measurements, normalised)
    got_direction = _derive_direction(docs, got_form, got_measurement, normalised)
    assert (got_form, got_measurement, got_direction) == (form_id, measurement_id, direction_id)


TIMEPOINT_FIXTURES = [
    ("Event-driven, trial is estimated to be up to 4.5 years", "event_driven"),
    ("Baseline, Week 24", "baseline_to_timepoint"),
    ("Baseline through Week 52", "cumulative_window"),
    ("Week 24", "single_fixed"),
    ("7 months", "bare_duration"),
    ("Weeks 28, 36, and 48", "multi_timepoint"),
    ("5 days following randomization", "anchored_offset"),
    ("90 ± 7 days", "visit_window"),
    ("Periprocedural", "event_relative"),
    ("Baseline", "baseline_only"),
]


def _classify_timepoint(docs, text):
    doc = docs["timepoint_pattern"]
    prepared = normalise(text).replace("+/-", "±").rstrip(".;")
    for word, value in doc["numeral_words"].items():
        prepared = re.sub(rf"\b{re.escape(word)}\b", str(value), prepared)
    for abbreviation, full in doc["abbreviations"].items():
        prepared = re.sub(rf"\b{re.escape(abbreviation)}\b", full, prepared)

    for term in sorted(doc["terms"], key=lambda t: t["priority"]):
        if any(re.search(g, prepared, re.I) for g in term.get("not_if_matches") or []):
            continue
        if any(re.search(p, prepared, re.I) for p in term.get("patterns") or []):
            return term["id"]
    return "unspecified"


@pytest.mark.parametrize("text,expected", TIMEPOINT_FIXTURES)
def test_timepoint_fixtures_classify(docs, text, expected):
    assert _classify_timepoint(docs, text) == expected


def test_bare_duration_is_not_read_as_a_fixed_timepoint(docs):
    """"6 weeks" is an observation period; "Week 6" is an assessment visit."""
    assert _classify_timepoint(docs, "6 weeks") == "bare_duration"
    assert _classify_timepoint(docs, "Week 6") == "single_fixed"
