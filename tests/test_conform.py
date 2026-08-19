"""Tests for the conforming pipeline (build-order step 3).

Acceptance criteria this file exists to check:

* the reference-table fixtures in test_vocab_loader.py conform end-to-end
  through the REAL pipeline (conform/pipeline.py), with match_method=exact,
* a deliberately garbled string lands in conformed.review_queue rather than
  being conformed at any confidence,
* timepoint classification stays at or above the coverage_on_sample baseline
  recorded in timepoint_patterns.yaml.

There is no live registry pull available in this sandbox (AACT/CT.gov are both
unreachable here -- see README's "A note on the ctgov_api backend"), so the
timepoint coverage check runs against the best available offline proxy for
"the sampled data": every worked example embedded in timepoint_patterns.yaml
itself, plus test_vocab_loader.py's TIMEPOINT_FIXTURES -- both drawn from the
same corpus review that produced the recorded baseline.
"""

from __future__ import annotations

import duckdb
import pytest

from clinical_endpoints.conform import resolve, semantic, text as textmod, threshold, timepoint
from clinical_endpoints.conform.pipeline import conform_row, run_conform
from clinical_endpoints.conform.rules import load_rules
from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, validate_vocab, write_vocab_tables

from tests.test_vocab_loader import REFERENCE_TABLE_FIXTURES, TIMEPOINT_FIXTURES


@pytest.fixture(scope="module")
def vocab_dir():
    return default_vocab_dir(__file__)


@pytest.fixture(scope="module")
def docs(vocab_dir):
    return load_vocab(vocab_dir)


@pytest.fixture(scope="module")
def _loaded_connection(vocab_dir, docs):
    """Writing vocab.* from measurements.yaml's 165 terms is the expensive part
    of setup; do it once per module and let `con` reset only the mutable
    raw/conformed state per test."""
    connection = duckdb.connect(":memory:")
    for schema in ("raw", "vocab", "conformed"):
        connection.execute(f"CREATE SCHEMA {schema}")
    assert validate_vocab(docs).errors == []
    write_vocab_tables(connection, docs, vocab_dir=vocab_dir)
    connection.execute(
        "CREATE TABLE raw.design_outcomes "
        "(nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR, time_frame VARCHAR, "
        "description VARCHAR, population VARCHAR)"
    )
    return connection


@pytest.fixture
def con(_loaded_connection):
    _loaded_connection.execute("DELETE FROM raw.design_outcomes")
    _loaded_connection.execute("DROP TABLE IF EXISTS conformed.endpoints")
    _loaded_connection.execute("DROP TABLE IF EXISTS conformed.review_queue")
    return _loaded_connection


def _insert_outcomes(con, rows):
    con.executemany(
        "INSERT INTO raw.design_outcomes (nct_id, outcome_type, measure, time_frame, description, population) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


# --------------------------------------------------- reference-table fixtures


def test_reference_table_fixtures_conform_end_to_end(con):
    rows = [
        (f"NCT{i:06d}", "primary", text, None, None, None)
        for i, (text, _form, _measurement, _direction) in enumerate(REFERENCE_TABLE_FIXTURES)
    ]
    _insert_outcomes(con, rows)

    summary = run_conform(con)
    assert summary["rows_queued"] == 0
    assert summary["rows_conformed"] == len(REFERENCE_TABLE_FIXTURES)

    conformed = {
        nct: (form_id, measurement_id, direction_id, form_method, measurement_method)
        for nct, form_id, measurement_id, direction_id, form_method, measurement_method in con.execute(
            "SELECT nct_id, form_id, measurement_id, direction_id, form_match_method, measurement_match_method "
            "FROM conformed.endpoints"
        ).fetchall()
    }

    for i, (text, expected_form, expected_measurement, expected_direction) in enumerate(REFERENCE_TABLE_FIXTURES):
        nct_id = f"NCT{i:06d}"
        form_id, measurement_id, direction_id, form_method, measurement_method = conformed[nct_id]
        assert (form_id, measurement_id, direction_id) == (expected_form, expected_measurement, expected_direction), text
        # Every fixture resolves straight from `measure` -- match_method=exact,
        # except forms that legitimately fall back to not_stated (a bare
        # instrument name genuinely states no form; that fallback is not an
        # "exact" match and is not one of these fixtures' claim).
        if expected_form != "not_stated":
            assert form_method == "exact", f"{text!r}: form matched via {form_method!r}, not exact"
        assert measurement_method == "exact", f"{text!r}: measurement matched via {measurement_method!r}, not exact"


# ------------------------------------------------------------- review queue


@pytest.mark.parametrize(
    "garbled",
    [
        "Zzqxv Wibble Frotz Blorpington",
        "asdkjfh 9932 xqzv nnnnnnn",
        "☃☃☃ nonsense-glyph-string ☃☃☃",
    ],
)
def test_garbled_measure_lands_in_review_queue_not_conformed(con, garbled):
    _insert_outcomes(con, [("NCT999999", "primary", garbled, None, None, None)])
    summary = run_conform(con)
    assert summary["rows_queued"] == 1
    assert summary["rows_conformed"] == 0

    assert con.execute("SELECT count(*) FROM conformed.endpoints").fetchone()[0] == 0
    row = con.execute(
        "SELECT reason, status, measure_raw FROM conformed.review_queue WHERE nct_id = 'NCT999999'"
    ).fetchone()
    assert row == ("measurement_unmatched", "pending", garbled)


def test_run_conform_is_idempotent_on_unchanged_rows(con):
    _insert_outcomes(con, [("NCT1", "primary", "Progression-Free Survival (PFS)", None, None, None)])
    first = run_conform(con)
    first_id = con.execute("SELECT endpoint_id FROM conformed.endpoints").fetchone()[0]
    second = run_conform(con)
    second_id = con.execute("SELECT endpoint_id FROM conformed.endpoints").fetchone()[0]
    assert first == second
    assert first_id == second_id


def test_conform_requires_pull_and_vocab_validate_first():
    empty_con = duckdb.connect(":memory:")
    for schema in ("raw", "vocab", "conformed"):
        empty_con.execute(f"CREATE SCHEMA {schema}")
    with pytest.raises(ValueError, match="pull"):
        run_conform(empty_con)


# ---------------------------------------------------------- timepoint parser


@pytest.mark.parametrize("text,expected", TIMEPOINT_FIXTURES)
def test_timepoint_fixtures_classify_via_real_pipeline(con, text, expected):
    rules = load_rules(con)
    result = timepoint.classify(text, rules.timepoint_rules)
    assert result.pattern_id == expected


def test_bare_duration_vs_single_fixed_via_real_pipeline(con):
    rules = load_rules(con)
    assert timepoint.classify("6 weeks", rules.timepoint_rules).pattern_id == "bare_duration"
    assert timepoint.classify("Week 6", rules.timepoint_rules).pattern_id == "single_fixed"


def test_timepoint_coverage_meets_recorded_baseline(con, docs):
    """The best offline proxy for "the sampled data": every worked example in
    timepoint_patterns.yaml plus TIMEPOINT_FIXTURES, checked against the
    classified_pct baseline the file itself records."""
    rules = load_rules(con)
    baseline_pct = docs["timepoint_pattern"]["coverage_on_sample"]["round_two"]["classified_pct_on_joined_export"]

    texts = {text for text, _expected in TIMEPOINT_FIXTURES}
    for term in docs["timepoint_pattern"]["terms"]:
        texts.update(term.get("examples") or [])
        texts.update(term.get("examples_round_two") or [])

    classified = sum(
        1 for text in texts if timepoint.classify(text, rules.timepoint_rules).pattern_id != rules.timepoint_rules.fallback
    )
    classified_pct = 100 * classified / len(texts)
    assert classified_pct >= baseline_pct, f"{classified_pct:.1f}% < recorded baseline {baseline_pct}%"


def test_timepoint_disambiguation_lets_form_break_the_through_up_to_tie(con):
    rules = load_rules(con)
    base = timepoint.classify("Baseline through Week 52", rules.timepoint_rules)
    assert base.pattern_id == "baseline_to_timepoint"

    cumulative_form = timepoint.apply_disambiguation(base, "incidence_proportion", rules.timepoint_rules)
    assert cumulative_form.pattern_id == "cumulative_window"

    change_form = timepoint.apply_disambiguation(base, "change_from_baseline", rules.timepoint_rules)
    assert change_form.pattern_id == "baseline_to_timepoint"


# --------------------------------------------------------- threshold parser


@pytest.mark.parametrize(
    "text,comparator,value,unit",
    [
        ("Proportion of participants achieving PASI75", ">=", 75.0, "%"),
        ("Proportion of participants achieving ACR20 response", ">=", 20.0, "%"),
        ("Proportion of subjects with HbA1c < 7%", "<", 7.0, "%"),
        ("Proportion of participants with at least 30% reduction in tumour size", ">=", 30.0, "%"),
        ("Response rate", None, None, None),
    ],
)
def test_threshold_parser(text, comparator, value, unit):
    result = threshold.parse_threshold(text)
    assert (result.comparator, result.value, result.unit) == (comparator, value, unit)


def test_threshold_only_populated_when_form_expects_it(con):
    rows = [
        ("NCT_T1", "primary", "Proportion of participants achieving PASI75", None, None, None),  # responder_proportion
        ("NCT_T2", "primary", "Overall Survival (OS)", None, None, None),  # time_to_event, no threshold expected
    ]
    _insert_outcomes(con, rows)
    run_conform(con)
    t1 = con.execute("SELECT threshold_comparator, threshold_value FROM conformed.endpoints WHERE nct_id = 'NCT_T1'").fetchone()
    t2 = con.execute("SELECT threshold_comparator, threshold_value FROM conformed.endpoints WHERE nct_id = 'NCT_T2'").fetchone()
    assert t1 == (">=", 75.0)
    assert t2 == (None, None)


# --------------------------------------------------------------- disambiguation


def test_form_disambiguation_uses_measurement_event_polarity_not_wording(con):
    """Genuinely ambiguous wording ("proportion ... with ... improvement") that
    matches BOTH responder_proportion and incidence_proportion must resolve on
    the matched measurement's event_polarity/domain, not on precedence order
    alone -- see vocab/README.md decision #4."""
    rules = load_rules(con)
    raw = "Proportion of participants with improvement in adverse event severity"
    normalised = textmod.normalise(raw, rules.normalisation_steps)
    fields = {"measure": normalised, "description": "", "time_frame": ""}

    assert rules.form_matcher.term_matches("responder_proportion", normalised) is not None
    assert rules.form_matcher.term_matches("incidence_proportion", normalised) is not None

    harm_measurement = resolve.FieldMatch("adverse_event", "exact", 1.0, "measure")
    benign_measurement = resolve.FieldMatch("acr_response_composite", "exact", 1.0, "measure")

    assert resolve.resolve_form(rules, fields, harm_measurement).term_id == "incidence_proportion"
    assert resolve.resolve_form(rules, fields, benign_measurement).term_id == "responder_proportion"


def test_form_upgraded_from_not_stated_via_time_frame_reference(con):
    """references.yaml's `implies_form`: a bare instrument name with no form in
    `measure`/`description` can be upgraded from a reference matched in
    `time_frame` -- recorded as syntactic_rule, never exact."""
    rules = load_rules(con)
    row = {
        "nct_id": "NCT_UP1", "outcome_type": "primary", "measure": "HbA1c",
        "time_frame": "From baseline to Week 24", "description": None, "population": None,
    }
    result = conform_row(rules, row, ta_id=None)
    assert result.form_id == "change_from_baseline"
    assert result.form_match_method == "syntactic_rule"
    assert result.form_source_field == "time_frame"


# ------------------------------------------------------------- semantic fallback


def test_semantic_fallback_needs_real_token_overlap():
    from clinical_endpoints.vocab.loader import default_vocab_dir as _dvd

    con = duckdb.connect(":memory:")
    for schema in ("raw", "vocab", "conformed"):
        con.execute(f"CREATE SCHEMA {schema}")
    vocab_dir = _dvd(__file__)
    docs = load_vocab(vocab_dir)
    write_vocab_tables(con, docs, vocab_dir=vocab_dir)

    index = semantic.build_semantic_index(con, "measurement", "measurements")
    assert semantic.best_match("completely unrelated gibberish zzzqx", index) is None
    assert semantic.best_match("", index) is None
