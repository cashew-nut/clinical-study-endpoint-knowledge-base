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

from clinical_endpoints.conform import direction as direction_mod
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

#: docs/EVENT_SEMANTICS_SPEC.md's synonym migration: PFS/OS/TTR's endpoint-NAME
#: synonyms moved out of measurements.yaml into named_endpoints.yaml, so these
#: rows' measurement now resolves via the step-0 named-endpoint match (its
#: default_measurement filling a cascade that is legitimately silent) rather
#: than a direct synonym hit -- match_method=named_endpoint, not exact. The
#: measurement id itself is unchanged (checked above via the fixture's own
#: expected_measurement), which is the zero measurement-id-churn invariant;
#: only the provenance got more honest, exactly as the migration note in each
#: measurement's `notes` says it would.
NAMED_ENDPOINT_MEASUREMENT_FIXTURES = frozenset(
    {"Progression-Free Survival (PFS)", "Overall Survival (OS)", "2-Year Overall Survival (OS)", "Time to Response (TTR)"}
)


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
        expected_measurement_method = "named_endpoint" if text in NAMED_ENDPOINT_MEASUREMENT_FIXTURES else "exact"
        assert measurement_method == expected_measurement_method, (
            f"{text!r}: measurement matched via {measurement_method!r}, not {expected_measurement_method!r}"
        )


# ------------------------------------------------- specific-vs-generic instrument


@pytest.mark.parametrize(
    "text",
    [
        "Change From Baseline in Health-Related Quality of Life (HRQoL) as Assessed by the EORTC QLQ-C30",
        "Quality of Life as measured by EORTC QLQ-C30",
    ],
)
def test_eortc_qlq_c30_beats_the_generic_hrqol_catch_all(con, text):
    """The generic `health_related_quality_of_life_unspecified` catch-all lists
    "health-related quality of life" (30 chars) among its synonyms, which is a
    longer matched span than "EORTC QLQ-C30" (13 chars) -- so before its
    `not_if_matches` veto, longest-match-wins handed rows that explicitly name
    the instrument to the catch-all instead. See that term's notes."""
    _insert_outcomes(con, [("NCT000900", "primary", text, None, None, None)])
    run_conform(con)
    measurement_id = con.execute(
        "SELECT measurement_id FROM conformed.endpoints WHERE nct_id = 'NCT000900'"
    ).fetchone()[0]
    assert measurement_id == "eortc_qlq_c30"


@pytest.mark.parametrize(
    "text,expected_measurement",
    [
        # Same failure mode as EORTC above, now guarded for the other named
        # instruments sharing `health_related_quality_of_life_unspecified`'s
        # generic synonyms.
        ("Health-Related Quality of Life as Measured by FACT-ES", "fact_es"),
        ("Time to Deterioration of Health-Related Quality of Life via CANKADO active", "cankado_qlq"),
        ("Quality of Life and Fear of Cancer Recurrence (FCRI-SF)", "fear_of_cancer_recurrence"),
        # New terms added from the missing-measures review.
        ("ECOG Performance Status", "ecog_performance_status"),
        ("Generalized Anxiety Disorder scale (GAD-7)", "gad7"),
        ("Patient Health Questionnaire-8 (PHQ8)", "phq8"),
        ("Self-Reported Symptoms of Cannabis Use Disorder (SR-SCUD)", "cannabis_use_disorder_severity"),
        ("Absolute Lymphocyte Count", "lymphocyte_count"),
        ("Incidence of Treatment-Related Toxicity", "adverse_event"),
    ],
)
def test_missing_measures_review_terms_conform(con, text, expected_measurement):
    _insert_outcomes(con, [("NCT000901", "primary", text, None, None, None)])
    run_conform(con)
    measurement_id = con.execute(
        "SELECT measurement_id FROM conformed.endpoints WHERE nct_id = 'NCT000901'"
    ).fetchone()[0]
    assert measurement_id == expected_measurement


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


def test_run_conform_parallel_matches_serial(con):
    """conform_row is a pure function of (rules, one row), so forcing the
    parallel path with --jobs must land byte-for-byte the same
    conformed.endpoints/review_queue as the serial path -- splitting the row
    list across processes may change how long this takes, never what it
    produces."""
    rows = [
        (f"NCT{i:06d}", "primary", text, None, None, None)
        for i, (text, _form, _measurement, _direction) in enumerate(REFERENCE_TABLE_FIXTURES)
    ] + [("NCT999999", "primary", "Zzqxv Wibble Frotz Blorpington", None, None, None)]
    _insert_outcomes(con, rows)

    serial = run_conform(con, jobs=1)
    serial_endpoints = con.execute(
        "SELECT endpoint_id, form_id, measurement_id, direction_id, timepoint_pattern "
        "FROM conformed.endpoints ORDER BY endpoint_id"
    ).fetchall()
    serial_queue = con.execute("SELECT review_id FROM conformed.review_queue ORDER BY review_id").fetchall()

    parallel = run_conform(con, jobs=2)
    parallel_endpoints = con.execute(
        "SELECT endpoint_id, form_id, measurement_id, direction_id, timepoint_pattern "
        "FROM conformed.endpoints ORDER BY endpoint_id"
    ).fetchall()
    parallel_queue = con.execute("SELECT review_id FROM conformed.review_queue ORDER BY review_id").fetchall()

    assert parallel["workers"] == 2
    assert serial["rows_conformed"] == parallel["rows_conformed"]
    assert serial["rows_queued"] == parallel["rows_queued"]
    assert serial_endpoints == parallel_endpoints
    assert serial_queue == parallel_queue


def test_run_conform_reports_progress(con):
    rows = [(f"NCT{i:06d}", "primary", "Progression-Free Survival (PFS)", None, None, None) for i in range(3)]
    _insert_outcomes(con, rows)

    calls = []
    run_conform(con, on_progress=lambda done, total: calls.append((done, total)))

    assert calls[0] == (0, 3)
    assert calls[-1] == (3, 3)
    assert [done for done, _total in calls] == sorted(done for done, _total in calls)


@pytest.mark.parametrize(
    "jobs,row_count,expected",
    [
        (0, 0, 1),
        (0, 5, 1),  # below _MIN_ROWS_FOR_PARALLEL: not worth spawning workers
        (1, 5000, 1),  # explicit --jobs 1 always forces serial
        (3, 5, 3),  # an explicit --jobs wins even under the auto threshold
        (3, 2, 2),  # ...but never more workers than there are rows
    ],
)
def test_resolve_worker_count(jobs, row_count, expected):
    from clinical_endpoints.conform.pipeline import _resolve_worker_count

    assert _resolve_worker_count(jobs, row_count) == expected


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


# ------------------------------------------- event semantics (docs/EVENT_SEMANTICS_SPEC.md)


def _row(measure, time_frame=None, description=None, nct_id="NCTX", outcome_type="primary", population=None):
    return {
        "nct_id": nct_id, "outcome_type": outcome_type, "measure": measure,
        "time_frame": time_frame, "description": description, "population": population,
    }


def test_nct01777919_pfs_row_resolves_the_progression_or_death_event(con):
    """The regression test the incident earns: NCT01777919's primary outcome,
    which used to project 'Time from randomisation to Tumour burden (RECIST)'
    -- the wrong-endpoint-definition defect this whole spec exists to fix."""
    rules = load_rules(con)
    row = _row("Progression-free survival", time_frame="6 months", nct_id="NCT01777919")
    result = conform_row(rules, row, ta_id=None, allocation="Randomized")

    assert result.form_id == "time_to_event"
    assert result.form_match_method == "exact"  # forms.yaml's own PFS synonym, untouched by the migration
    assert result.measurement_id == "tumour_burden_recist"
    assert result.measurement_match_method == "named_endpoint"
    assert result.event_id == "disease_progression_or_death"
    assert result.event_match_method == "named_endpoint"
    assert result.reference_id == "randomisation"
    assert result.reference_match_method == "named_endpoint"
    assert result.named_endpoint_id == "pfs"
    assert result.direction_id == "longer_is_better"
    assert result.event_polarity_used == "harm"


def test_nct01777919_os_row_resolves_the_death_event(con):
    rules = load_rules(con)
    row = _row("Overall survival", time_frame="2 years", nct_id="NCT01777919", outcome_type="secondary")
    result = conform_row(rules, row, ta_id=None, allocation="Randomized")

    assert result.form_id == "time_to_event"
    assert result.measurement_id == "vital_status"
    assert result.measurement_match_method == "named_endpoint"
    assert result.event_id == "death_any_cause"
    assert result.event_match_method == "named_endpoint"
    assert result.reference_id == "randomisation"
    assert result.named_endpoint_id == "os"
    assert result.direction_id == "longer_is_better"


def test_pfs_ttp_dor_share_measurement_but_resolve_distinct_event_reference_pairs(con):
    """PFS, TTP and DOR all conform to tumour_burden_recist -- preserving the
    SAME_MEASUREMENT_DIFFERENT_FORM join with ORR -- but must stop being each
    other's twins on (event, reference)."""
    rules = load_rules(con)

    def resolve_named(measure):
        return conform_row(rules, _row(measure), ta_id=None, allocation="Randomized")

    pfs = resolve_named("Progression-free survival")
    ttp = resolve_named("Time to progression")
    dor = resolve_named("Duration of response")
    orr = resolve_named("Objective response rate")

    assert {pfs.measurement_id, ttp.measurement_id, dor.measurement_id, orr.measurement_id} == {"tumour_burden_recist"}

    pairs = {
        (pfs.event_id, pfs.reference_id),
        (ttp.event_id, ttp.reference_id),
        (dor.event_id, dor.reference_id),
    }
    assert len(pairs) == 3, f"PFS/TTP/DOR must not collapse to fewer than 3 distinct (event, reference) pairs: {pairs}"
    assert (pfs.event_id, pfs.reference_id) == ("disease_progression_or_death", "randomisation")
    assert (ttp.event_id, ttp.reference_id) == ("disease_progression", "randomisation")
    assert (dor.event_id, dor.reference_id) == ("disease_progression_or_death", "response_onset")


@pytest.mark.parametrize("allocation", [None, "Non-Randomized", "Single Group Assignment"])
def test_single_arm_pfs_does_not_default_reference_to_randomisation(con, allocation):
    """A named-endpoint definition's `reference` is applied only when
    raw.studies.allocation says the trial is randomised -- asserting
    "from randomisation" on a single-arm trial would be exactly the
    unannounced-default disease docs/USDM_PROJECTION_INTEGRITY_SPEC.md exists
    to cure."""
    rules = load_rules(con)
    row = _row("Progression-free survival")
    result = conform_row(rules, row, ta_id=None, allocation=allocation)

    assert result.named_endpoint_id == "pfs"
    assert result.measurement_id == "tumour_burden_recist"  # default_measurement still fills -- unconditional
    assert result.reference_id == "not_stated"
    assert result.reference_match_method is None


def test_single_arm_pfs_reference_still_resolves_from_explicit_text(con):
    """The definition's reference only fills silence; text that itself states
    a reference still wins, randomised or not."""
    rules = load_rules(con)
    row = _row("Progression-free survival", time_frame="From first dose")
    result = conform_row(rules, row, ta_id=None, allocation="Non-Randomized")
    assert result.reference_id == "treatment_start"
    # time_frame is reference's PRIMARY field (matching.yaml's cascade reads it
    # first, exact), not a secondary inference the way it is for form/measurement.
    assert result.reference_match_method == "exact"


def test_event_family_row_with_unresolvable_event_falls_to_not_stated_not_the_measurement(con):
    """The defect this spec exists to kill: an event-family row whose event
    does not resolve must carry event_id = not_stated, never silently borrow
    the measurement into the event slot."""
    rules = load_rules(con)
    row = _row("Time to RECIST assessment")
    result = conform_row(rules, row, ta_id=None, allocation=None)

    assert result.form_id == "time_to_event"
    assert result.measurement_id == "tumour_burden_recist"
    assert result.event_id == "not_stated"
    assert result.event_match_method is None
    assert result.named_endpoint_id is None


def test_event_id_is_null_not_not_stated_for_non_event_family_forms(con):
    """change_from_baseline is not event_family: a row of that form has no
    event, full stop -- NULL, not an unresolved 'not_stated'."""
    rules = load_rules(con)
    row = _row("Change from baseline in Hemoglobin A1c (HbA1c)")
    result = conform_row(rules, row, ta_id=None, allocation=None)

    assert result.form_id == "change_from_baseline"
    assert result.event_id is None
    assert result.event_match_method is None
    assert result.event_confidence is None
    assert result.event_source_field is None


def test_measurement_implies_event_resolves_with_no_new_text_matching(con):
    """A row whose MEASUREMENT is itself event-shaped (vital_status) resolves
    its event via `implies_event`, with no named-endpoint or events.yaml text
    match involved -- e.g. plain "time to death", which names no PFS/OS-style
    literature acronym at all."""
    rules = load_rules(con)
    row = _row("Time to death")
    result = conform_row(rules, row, ta_id=None, allocation="Randomized")

    assert result.named_endpoint_id is None
    assert result.measurement_id == "vital_status"
    assert result.event_id == "death_any_cause"
    # Matched directly via events.yaml's death_any_cause pattern (step 4b),
    # which fires before vital_status's implies_event (step 4c) is consulted.
    assert result.event_match_method in ("exact", "syntactic_rule")


def test_derive_direction_prefers_resolved_event_polarity_over_cues(con):
    """docs/EVENT_SEMANTICS_SPEC.md step 5: event polarity first. Deliberately
    passes cue_text that WOULD read as a harm cue ("time to ... death") together
    with a benefit-polarity event, to prove the event wins rather than merely
    agreeing with the cues by coincidence."""
    rules = load_rules(con)
    result = direction_mod.derive_direction(
        "time_to_event", "vital_status", "time to death", rules.direction_rules,
        event_id="response_onset",
    )
    assert result.event_polarity_used == "benefit"
    assert result.direction_id == "shorter_is_better"


def test_derive_direction_falls_back_to_cues_when_event_id_is_none_or_not_stated(con):
    """Non-event-family rows (event_id=None) and event-family rows whose event
    itself stayed not_stated must derive direction exactly as they did before
    this spec -- the existing cue/measurement cascade, untouched."""
    rules = load_rules(con)
    for event_id in (None, "not_stated"):
        result = direction_mod.derive_direction(
            "time_to_event", "tumour_burden_recist", "time to progression", rules.direction_rules,
            event_id=event_id,
        )
        assert result.direction_id == "longer_is_better"
        assert result.event_polarity_used == "harm"


def test_direction_regression_across_the_full_reference_table(con):
    """Every pre-existing fixture's direction, re-derived through the REAL
    pipeline under event-first derivation, must be byte-identical to what it
    was before events existed -- "direction never flips on a currently-correct
    row" as an executable assertion, not just a hope."""
    from tests.test_vocab_loader import REFERENCE_TABLE_FIXTURES

    rows = [
        _row(text, nct_id=f"NCTD{i:06d}")
        for i, (text, _form, _measurement, _direction) in enumerate(REFERENCE_TABLE_FIXTURES)
    ]
    _insert_outcomes(con, [(r["nct_id"], r["outcome_type"], r["measure"], r["time_frame"], r["description"], r["population"]) for r in rows])
    run_conform(con)

    directions = dict(con.execute("SELECT nct_id, direction_id FROM conformed.endpoints").fetchall())
    for i, (text, _form, _measurement, expected_direction) in enumerate(REFERENCE_TABLE_FIXTURES):
        assert directions[f"NCTD{i:06d}"] == expected_direction, text
