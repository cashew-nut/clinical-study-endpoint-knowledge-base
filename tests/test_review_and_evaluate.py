"""Human review and accuracy measurement.

The properties under test are the ones that decide whether a number produced by this
system can be believed: that a reviewer's decision outlives the logic it corrects but
not the text it was about, that a gold annotation is never scored against words its
annotator did not read, and that a metric which is undefined says so rather than
reporting zero.
"""

from __future__ import annotations

import json
import shutil

import pytest
import yaml

from ceskb.classify.engine import classify_all, classify_outcome
from ceskb.evaluate.gold import (
    NO_MATCH,
    GoldError,
    agreement,
    check_gold_set,
    cohens_kappa,
    load_gold_set,
    load_gold_sets,
)
from ceskb.evaluate.score import check_thresholds, persist, score
from ceskb.review.overrides import (
    KEEP_CONCEPT,
    Override,
    OverrideError,
    active_overrides,
    check_overrides,
    load_overrides,
    load_overrides_into_db,
    record_override,
    save_overrides,
)
from ceskb.store.db import connect


@pytest.fixture
def writable_db(tmp_path, built_db):
    """A fresh, writable copy of the pipeline output, per test.

    Copied from the session-scoped build rather than re-derived: these tests mutate the
    database, so they need their own, but rebuilding it for each is pure waste.
    """
    path = tmp_path / "review.duckdb"
    shutil.copy(built_db, path)
    return path


@pytest.fixture
def overrides_file(tmp_path):
    return tmp_path / "overrides.yaml"


def _outcome(conn, uid):
    """Rebuild an OutcomeRecord exactly as the pipeline would, therapeutic areas included.

    The areas matter: rules may be scoped to one, so omitting them would quietly test a
    different classification than the pipeline performs.
    """
    from ceskb.classify.engine import OutcomeRecord

    row = conn.execute(
        """
        SELECT o.outcome_uid, o.study_id, o.endpoint_level, coalesce(o.measure,''),
               coalesce(o.description,''), coalesce(o.time_frame,''),
               coalesce(s.therapeutic_areas, '[]')
        FROM study_outcome o LEFT JOIN study s USING (study_id)
        WHERE o.outcome_uid = ?
        """,
        [uid],
    ).fetchone()
    return OutcomeRecord(*row[:6], therapeutic_areas=tuple(json.loads(row[6])))


# --------------------------------------------------------------------------- #
# overrides: precedence
# --------------------------------------------------------------------------- #
def test_override_may_change_a_defining_axis(vocab, built_db):
    """What an extractor is forbidden to do, a reviewer is allowed to do.

    Defining axes are locked against regexes precisely because a title is weak
    evidence about what an endpoint fundamentally is. A human who has read the
    protocol is not weak evidence, so the lock must not apply to them.
    """
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0001:primary:0")

    derived = classify_outcome(outcome, vocab)
    assert derived is not None
    assert derived.axes["endpoint_form"][0] == "time_to_event"

    override = Override(
        outcome_uid=outcome.outcome_uid,
        reason="Reviewed against the protocol: analysed as a landmark rate.",
        reviewer="tester",
        decided_at="2026-08-17T00:00:00+00:00",
        axes={"endpoint_form": "responder_binary"},
    )
    corrected = classify_outcome(outcome, vocab, override)
    assert corrected is not None
    term, origin, evidence = corrected.axes["endpoint_form"]
    assert (term, origin) == ("responder_binary", "human_override")
    assert "tester" in evidence
    assert corrected.overridden is True


def test_override_to_a_different_concept_reseeds_its_structure(vocab, built_db):
    """Correcting the concept must not leave the rejected concept's structure behind."""
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0001:primary:0")

    override = Override(
        outcome_uid=outcome.outcome_uid,
        reason="Reviewed: this is the response rate, not survival.",
        reviewer="tester",
        decided_at="2026-08-17T00:00:00+00:00",
        concept_id="ORR",
    )
    spec = classify_outcome(outcome, vocab, override)
    assert spec is not None
    assert spec.concept_id == "ORR"
    orr = vocab.concept("ORR")
    # Every defining axis now comes from ORR, not from PFS.
    assert spec.axes["endpoint_form"][0] == orr.structure["endpoint_form"]
    assert spec.axes["measurement_concept"][0] == orr.structure["measurement_concept"]
    assert spec.axes["endpoint_form"][1] == "human_override"


def test_override_can_suppress_a_match_entirely(vocab, built_db):
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0001:primary:0")

    override = Override(
        outcome_uid=outcome.outcome_uid,
        reason="Reviewed: no concept in the vocabulary describes this.",
        reviewer="tester",
        decided_at="2026-08-17T00:00:00+00:00",
        concept_id=None,
    )
    assert override.suppresses is True
    assert classify_outcome(outcome, vocab, override) is None


def test_override_can_classify_what_no_rule_matched(vocab, built_db):
    """The most valuable correction: a reviewer supplying a concept where rules were silent."""
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0021:secondary:1")

    assert classify_outcome(outcome, vocab) is None, "expected this outcome to be unmatched"

    override = Override(
        outcome_uid=outcome.outcome_uid,
        reason="Reviewed against the protocol: this is the objective response rate.",
        reviewer="tester",
        decided_at="2026-08-17T00:00:00+00:00",
        concept_id="ORR",
    )
    spec = classify_outcome(outcome, vocab, override)
    assert spec is not None
    assert spec.concept_id == "ORR"
    assert spec.selected_rule_id is None
    assert spec.overridden is True


# --------------------------------------------------------------------------- #
# overrides: durability and staleness
# --------------------------------------------------------------------------- #
def test_override_survives_a_derivation_version_bump(
    vocab, writable_db, overrides_file, monkeypatch
):
    """The whole point of keying on the outcome rather than the spec.

    A spec_id is a function of DERIVATION_VERSION. Keying overrides on it would discard
    every human decision the moment a rule changed -- exactly when they matter most.
    """
    uid = "SYNTH-0001:secondary:0"
    with connect(writable_db) as conn:
        record_override(
            conn,
            outcome_uid=uid,
            reason="Reviewed against the protocol; recorded for durability.",
            reviewer="tester",
            concept_id="CR_RATE",
            path=overrides_file,
        )
        before = conn.execute(
            "SELECT spec_id FROM endpoint_spec WHERE outcome_uid = ?", [uid]
        ).fetchone()[0]

    # Simulate a rule change invalidating every derived spec_id.
    import ceskb.classify.engine as engine

    monkeypatch.setattr(engine, "DERIVATION_VERSION", "9999.01.01.1")
    with connect(writable_db) as conn:
        classify_all(conn, vocab=vocab)
        after, concept_id, overridden = conn.execute(
            "SELECT spec_id, concept_id, overridden FROM endpoint_spec WHERE outcome_uid = ?",
            [uid],
        ).fetchone()

    assert after != before, "the spec was expected to be re-derived under a new version"
    assert concept_id == "CR_RATE", "the reviewer's decision did not survive re-derivation"
    assert overridden is True


def test_override_goes_stale_when_the_source_text_changes(writable_db, overrides_file):
    """A judgement about text that no longer exists must stop being applied."""
    uid = "SYNTH-0006:primary:0"
    with connect(writable_db) as conn:
        record_override(
            conn,
            outcome_uid=uid,
            reason="Reviewed against the protocol as recorded at the time.",
            reviewer="tester",
            concept_id="HBA1C_TARGET_RESPONDER",
            path=overrides_file,
        )
        assert uid in active_overrides(conn)

        # The registry rewrites the outcome; the hash moves.
        conn.execute(
            "UPDATE study_outcome SET record_hash = 'rewritten' WHERE outcome_uid = ?", [uid]
        )
        counts = load_overrides_into_db(conn, path=overrides_file)

        assert counts["stale"] == 1
        assert counts["active"] == 0
        assert uid not in active_overrides(conn)
        status = conn.execute(
            "SELECT status FROM spec_override WHERE outcome_uid = ?", [uid]
        ).fetchone()[0]
        assert status == "stale"


def test_override_for_an_outcome_outside_this_scope_is_not_applied(writable_db, overrides_file):
    save_overrides(
        [
            Override(
                outcome_uid="NCT99999999:primary:0",
                reason="A decision made in a differently scoped database.",
                reviewer="tester",
                decided_at="2026-08-17T00:00:00+00:00",
                concept_id="ORR",
            )
        ],
        overrides_file,
    )
    with connect(writable_db) as conn:
        counts = load_overrides_into_db(conn, path=overrides_file)
        assert counts["orphaned"] == 1
        assert active_overrides(conn) == {}


def test_retired_override_keeps_its_audit_trail_but_stops_applying(writable_db, overrides_file):
    save_overrides(
        [
            Override(
                outcome_uid="SYNTH-0006:primary:0",
                reason="Superseded by a rule change; kept for the audit trail.",
                reviewer="tester",
                decided_at="2026-08-17T00:00:00+00:00",
                concept_id="ORR",
                status="retired",
            )
        ],
        overrides_file,
    )
    with connect(writable_db) as conn:
        counts = load_overrides_into_db(conn, path=overrides_file)
        assert counts["retired"] == 1
        assert active_overrides(conn) == {}
        assert conn.execute("SELECT count(*) FROM spec_override").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# overrides: file handling
# --------------------------------------------------------------------------- #
def test_override_file_round_trips(overrides_file):
    original = [
        Override(
            outcome_uid="SYNTH-0001:primary:0",
            reason="A reason long enough to satisfy the schema.",
            reviewer="tester",
            decided_at="2026-08-17T00:00:00+00:00",
            concept_id="ORR",
            axes={"endpoint_form": "responder_binary"},
            source_hash="abc123",
        )
    ]
    save_overrides(original, overrides_file)
    assert load_overrides(overrides_file) == original


def test_absent_override_file_is_simply_no_overrides(tmp_path):
    assert load_overrides(tmp_path / "nothing.yaml") == []


def test_two_decisions_for_one_outcome_are_rejected(overrides_file):
    overrides_file.write_text(
        yaml.safe_dump(
            {
                "overrides": [
                    {
                        "outcome_uid": "SYNTH-0001:primary:0",
                        "concept_id": "ORR",
                        "reason": "The first decision, long enough for the schema.",
                        "reviewer": "a",
                        "decided_at": "2026-08-17T00:00:00+00:00",
                    },
                    {
                        "outcome_uid": "SYNTH-0001:primary:0",
                        "concept_id": "PFS",
                        "reason": "A contradictory second decision, also long enough.",
                        "reviewer": "b",
                        "decided_at": "2026-08-17T00:00:00+00:00",
                    },
                ]
            }
        )
    )
    with pytest.raises(OverrideError, match="two overrides"):
        load_overrides(overrides_file)


def test_an_override_without_a_reason_is_rejected(overrides_file):
    """An unexplained override is not reviewable by the next person."""
    overrides_file.write_text(
        yaml.safe_dump(
            {
                "overrides": [
                    {
                        "outcome_uid": "SYNTH-0001:primary:0",
                        "concept_id": "ORR",
                        "reason": "wrong",
                        "reviewer": "a",
                        "decided_at": "2026-08-17T00:00:00+00:00",
                    }
                ]
            }
        )
    )
    with pytest.raises(OverrideError):
        load_overrides(overrides_file)


def test_override_typos_are_caught_against_the_vocabulary(vocab):
    problems = check_overrides(
        [
            Override(
                outcome_uid="x",
                reason="A reason long enough to satisfy the schema.",
                reviewer="t",
                decided_at="2026-08-17T00:00:00+00:00",
                concept_id="NOT_A_CONCEPT",
                axes={"endpoint_form": "not_a_term", "not_an_axis": "x"},
            )
        ],
        vocab,
    )
    assert any("unknown concept" in p for p in problems)
    assert any("not a term" in p for p in problems)
    assert any("not an overridable axis" in p for p in problems)


def test_recording_an_override_captures_hash_and_prior_verdict(writable_db, overrides_file):
    uid = "SYNTH-0001:secondary:0"
    with connect(writable_db) as conn:
        expected_hash = conn.execute(
            "SELECT record_hash FROM study_outcome WHERE outcome_uid = ?", [uid]
        ).fetchone()[0]
        prior = conn.execute(
            "SELECT concept_id FROM endpoint_spec WHERE outcome_uid = ?", [uid]
        ).fetchone()[0]
        override = record_override(
            conn,
            outcome_uid=uid,
            reason="Reviewed against the protocol and corrected.",
            reviewer="tester",
            concept_id="CR_RATE",
            path=overrides_file,
        )
    assert override.source_hash == expected_hash
    assert override.supersedes_concept_id == prior


def test_recording_against_an_unknown_outcome_is_rejected(writable_db, overrides_file):
    with connect(writable_db) as conn:
        with pytest.raises(OverrideError, match="no outcome"):
            record_override(
                conn,
                outcome_uid="NOT-A-REAL-OUTCOME",
                reason="A reason long enough to satisfy the schema.",
                reviewer="tester",
                concept_id="ORR",
                path=overrides_file,
            )


# --------------------------------------------------------------------------- #
# review queue
# --------------------------------------------------------------------------- #
def test_review_queue_surfaces_doubt_and_nothing_else(conn):
    rows = conn.execute(
        "SELECT reason, match_confidence, competing_rule_count, ambiguous_tie, unresolved_count "
        "FROM review_queue"
    ).fetchall()
    for reason, confidence, competing, tie, unresolved in rows:
        assert tie or competing > 0 or confidence < 0.7 or unresolved >= 3, (
            "a specification with no ground for doubt should not be queued"
        )
        assert reason in {
            "arbitrary_tie_break",
            "contested_match",
            "low_confidence",
            "unresolved_parameters",
        }


def test_review_queue_ranks_arbitrary_ties_above_everything_else(conn):
    """An arbitrary choice is the strongest reason to want a human."""
    rows = conn.execute(
        "SELECT ambiguous_tie, review_score FROM review_queue ORDER BY review_score DESC"
    ).fetchall()
    ties = [score for tie, score in rows if tie]
    others = [score for tie, score in rows if not tie]
    if ties and others:
        assert min(ties) > max(others)


def test_working_the_queue_shortens_it(writable_db, overrides_file):
    with connect(writable_db) as conn:
        before = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
        assert before > 0, "expected the fixture corpus to raise at least one doubt"
        uid = conn.execute("SELECT outcome_uid FROM review_queue LIMIT 1").fetchone()[0]
        record_override(
            conn,
            outcome_uid=uid,
            reason="Reviewed and confirmed against the protocol.",
            reviewer="tester",
            concept_id=conn.execute(
                "SELECT concept_id FROM endpoint_spec WHERE outcome_uid = ?", [uid]
            ).fetchone()[0],
            path=overrides_file,
        )
        after = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    assert after == before - 1


def _top_firing_rule(vocab, outcome):
    from ceskb.classify.engine import evaluate_rule
    from ceskb.classify.extractors import TextFields

    fields = TextFields(outcome.measure, outcome.description, outcome.time_frame)
    firing = [r for r in vocab.rules if evaluate_rule(r, fields) is not None]
    assert firing, "expected at least one rule to fire on this outcome"
    return max(firing, key=lambda r: (r.priority, r.confidence))


def _rigged_tie(vocab, outcome, concept_id):
    """A copy of the vocabulary with a rule that ties the winner at its exact rank.

    The fixture corpus contains no genuine cross-concept tie, which is a good property
    of the rule packs and an awkward one for testing the code that handles ties. So the
    tie is constructed rather than found.
    """
    import dataclasses

    top = _top_firing_rule(vocab, outcome)
    twin = dataclasses.replace(
        top,
        rule_id=f"{top.rule_id}.rigged_twin",
        concept_id=concept_id,
        therapeutic_area_hint=None,
    )
    return dataclasses.replace(vocab, rules=tuple(vocab.rules) + (twin,))


def test_an_arbitrary_tie_is_recorded_rather_than_hidden(vocab, built_db):
    """Two rules for different concepts at equal rank must be flagged, not silently sorted."""
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0001:secondary:0")

    baseline = classify_outcome(outcome, vocab)
    assert baseline is not None and baseline.ambiguous_tie is False

    top = _top_firing_rule(vocab, outcome)
    assert top.concept_id != "DFS"

    spec = classify_outcome(outcome, _rigged_tie(vocab, outcome, concept_id="DFS"))
    assert spec is not None
    assert spec.ambiguous_tie is True, "an arbitrary tie-break must be recorded"
    assert spec.competing_rule_count >= 1


def test_two_rules_at_equal_rank_for_the_same_concept_are_not_a_tie(vocab, built_db):
    """Nothing is at stake when the rules agree, so it is not something to review."""
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0001:secondary:0")

    top = _top_firing_rule(vocab, outcome)
    spec = classify_outcome(outcome, _rigged_tie(vocab, outcome, concept_id=top.concept_id))
    assert spec is not None
    assert spec.ambiguous_tie is False
    assert spec.concept_id == top.concept_id


def test_an_override_only_correcting_axes_is_not_a_suppression(vocab, built_db):
    """Three states, not two: no opinion about the concept is not 'nothing fits'."""
    with connect(built_db, read_only=True) as conn:
        outcome = _outcome(conn, "SYNTH-0001:primary:0")

    override = Override(
        outcome_uid=outcome.outcome_uid,
        reason="Protocol states the full analysis set; the registry text omits it.",
        reviewer="tester",
        decided_at="2026-08-17T00:00:00+00:00",
        axes={"analysis_population": "full_analysis_set"},
    )
    assert override.concept_id == KEEP_CONCEPT
    assert override.suppresses is False
    assert override.changes_concept is False

    spec = classify_outcome(outcome, vocab, override)
    assert spec is not None, "an axis-only correction must not suppress the match"
    assert spec.concept_id == "PFS"
    assert spec.axes["analysis_population"] == (
        "full_analysis_set",
        "human_override",
        "reviewer tester",
    )


def test_an_axis_only_override_round_trips_without_growing_a_concept(overrides_file):
    """The sentinel must not leak into the file as if it were a concept id."""
    override = Override(
        outcome_uid="SYNTH-0001:primary:0",
        reason="Protocol states the full analysis set; the registry text omits it.",
        reviewer="tester",
        decided_at="2026-08-17T00:00:00+00:00",
        axes={"analysis_population": "full_analysis_set"},
    )
    save_overrides([override], overrides_file)
    assert "concept_id" not in overrides_file.read_text()
    assert load_overrides(overrides_file) == [override]


def test_an_override_that_would_change_nothing_is_rejected(vocab):
    problems = check_overrides(
        [
            Override(
                outcome_uid="x",
                reason="A reason long enough to satisfy the schema.",
                reviewer="t",
                decided_at="2026-08-17T00:00:00+00:00",
            )
        ],
        vocab,
    )
    assert any("would change nothing" in p for p in problems)


# --------------------------------------------------------------------------- #
# gold sets and scoring
# --------------------------------------------------------------------------- #
def test_the_shipped_gold_set_loads_and_checks_out(vocab):
    sets = load_gold_sets()
    assert sets, "expected at least one gold set in review/gold"
    for gold in sets:
        assert check_gold_set(gold, vocab) == []


def test_scoring_the_shipped_gold_set_agrees_with_the_classifier(conn, vocab):
    """A regression pin. It is not evidence of accuracy -- the set is self-annotated."""
    gold = load_gold_sets()[0]
    assert gold.independence == "self_annotated"
    report = score(conn, gold)
    assert report["items_excluded"] == 0
    assert report["concept_accuracy"] == 1.0
    assert "caveat" in report, "a self-annotated set must carry its caveat"


def test_a_stale_annotation_is_excluded_rather_than_scored(writable_db, tmp_path):
    """The property that stops a gold set quietly re-pointing at rewritten text."""
    gold_path = tmp_path / "one.yaml"
    with connect(writable_db) as conn:
        uid, real_hash = conn.execute(
            "SELECT outcome_uid, record_hash FROM study_outcome LIMIT 1"
        ).fetchone()

    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "stale-test",
                "annotator": "tester",
                "independence": "independent",
                "items": [
                    {
                        "outcome_uid": uid,
                        "concept_id": "ORR",
                        "source_hash": "a-hash-from-before-the-text-changed",
                    }
                ],
            }
        )
    )
    with connect(writable_db, read_only=True) as conn:
        report = score(conn, load_gold_set(gold_path))

    assert report["items_scored"] == 0
    assert report["items_excluded"] == 1
    assert report["excluded"][0]["why"] == "source_text_changed"
    assert real_hash != "a-hash-from-before-the-text-changed"


def test_an_annotation_for_an_absent_outcome_is_excluded(conn, tmp_path):
    gold_path = tmp_path / "absent.yaml"
    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "absent-test",
                "annotator": "tester",
                "independence": "independent",
                "items": [{"outcome_uid": "NCT00000000:primary:0", "concept_id": "ORR"}],
            }
        )
    )
    report = score(conn, load_gold_set(gold_path))
    assert report["items_excluded"] == 1
    assert report["excluded"][0]["why"] == "absent_from_database"


def test_a_null_annotation_scores_as_a_real_judgement(conn, tmp_path):
    """Without null items a classifier scores perfectly by matching everything."""
    gold_path = tmp_path / "null.yaml"
    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "null-test",
                "annotator": "tester",
                "independence": "independent",
                "items": [{"outcome_uid": "SYNTH-0021:secondary:1", "concept_id": None}],
            }
        )
    )
    report = score(conn, load_gold_set(gold_path))
    assert report["items_scored"] == 1
    assert report["concept_accuracy"] == 1.0
    assert NO_MATCH in report["per_concept"]


def test_precision_is_undefined_not_zero_when_a_label_is_never_predicted(conn, tmp_path):
    """Reporting 0.0 would make a precision gate fail on an absence of evidence."""
    gold_path = tmp_path / "missed.yaml"
    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "missed-test",
                "annotator": "tester",
                "independence": "independent",
                # Annotated as DFS; the classifier says PFS. DFS is never predicted.
                "items": [{"outcome_uid": "SYNTH-0003:primary:0", "concept_id": "DFS"}],
            }
        )
    )
    report = score(conn, load_gold_set(gold_path))
    assert report["per_concept"]["DFS"]["precision"] is None
    assert report["per_concept"]["DFS"]["recall"] == 0.0
    assert check_thresholds(report, min_precision=0.9) == [], (
        "an undefined precision must not fail a precision gate"
    )
    assert check_thresholds(report, min_recall=0.9), "recall is what should fail here"


def test_thresholds_gate_on_each_concept_not_the_average(conn, tmp_path):
    gold_path = tmp_path / "gate.yaml"
    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "gate-test",
                "annotator": "tester",
                "independence": "independent",
                "items": [
                    {"outcome_uid": "SYNTH-0001:primary:0", "concept_id": "PFS"},
                    {"outcome_uid": "SYNTH-0001:primary:1", "concept_id": "OS"},
                    {"outcome_uid": "SYNTH-0003:primary:0", "concept_id": "OS"},
                ],
            }
        )
    )
    report = score(conn, load_gold_set(gold_path))
    # PFS is predicted for an outcome annotated OS, so PFS precision is 0.5 while
    # overall accuracy stays at two thirds.
    failures = check_thresholds(report, min_precision=0.9)
    assert any(f.startswith("PFS:") for f in failures)


def test_excluded_items_can_fail_the_gate(conn, tmp_path):
    """A score over a shrinking denominator stops meaning anything."""
    gold_path = tmp_path / "excl.yaml"
    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "excl-test",
                "annotator": "tester",
                "independence": "independent",
                "items": [
                    {"outcome_uid": "SYNTH-0001:primary:0", "concept_id": "PFS"},
                    {"outcome_uid": "NCT00000000:primary:0", "concept_id": "ORR"},
                ],
            }
        )
    )
    report = score(conn, load_gold_set(gold_path))
    assert check_thresholds(report, max_excluded=0)
    assert check_thresholds(report, max_excluded=1) == []


def test_axis_scoring_covers_only_the_axes_the_annotator_judged(conn, tmp_path):
    gold_path = tmp_path / "axes.yaml"
    gold_path.write_text(
        yaml.safe_dump(
            {
                "gold_set_id": "axis-test",
                "annotator": "tester",
                "independence": "independent",
                "items": [
                    {
                        "outcome_uid": "SYNTH-0001:primary:0",
                        "concept_id": "PFS",
                        "axes": {"endpoint_form": "time_to_event"},
                    }
                ],
            }
        )
    )
    report = score(conn, load_gold_set(gold_path))
    assert set(report["axis_accuracy"]) == {"endpoint_form"}
    assert report["axis_accuracy"]["endpoint_form"]["accuracy"] == 1.0


def test_evaluation_runs_are_persisted_as_a_series(writable_db):
    gold = load_gold_sets()[0]
    with connect(writable_db) as conn:
        first = persist(conn, score(conn, gold))
        second = persist(conn, score(conn, gold))
        rows = conn.execute("SELECT evaluation_id FROM evaluation_run").fetchall()
    assert {first, second} == {row[0] for row in rows}


def test_a_malformed_gold_set_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"gold_set_id": "x", "items": []}))
    with pytest.raises(GoldError):
        load_gold_set(bad)


# --------------------------------------------------------------------------- #
# inter-annotator agreement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pairs,expected",
    [
        ([("a", "a"), ("b", "b"), ("c", "c")], 1.0),
        ([("a", "a"), ("a", "a")], None),  # one category: chance agreement is total
        ([], None),
    ],
)
def test_kappa_edge_cases(pairs, expected):
    assert cohens_kappa(pairs) == expected


def test_kappa_discounts_agreement_explained_by_skew():
    """Raw percent agreement flatters a skewed label set; kappa should not."""
    # 9 of 10 items are 'a', and the annotators differ on the one that is not.
    pairs = [("a", "a")] * 9 + [("b", "a")]
    raw = sum(1 for x, y in pairs if x == y) / len(pairs)
    kappa = cohens_kappa(pairs)
    assert raw == 0.9
    assert kappa is not None and kappa < 0.5, (
        "kappa should be far below the raw agreement when one label dominates"
    )


def test_agreement_reports_disagreements_and_coverage(tmp_path):
    def _write(name, annotator, labels):
        path = tmp_path / name
        path.write_text(
            yaml.safe_dump(
                {
                    "gold_set_id": name,
                    "annotator": annotator,
                    "independence": "independent",
                    "items": [
                        {"outcome_uid": uid, "concept_id": concept, "text": f"text {uid}"}
                        for uid, concept in labels.items()
                    ],
                }
            )
        )
        return load_gold_set(path)

    first = _write("a.yaml", "alice", {"u1": "PFS", "u2": "OS", "u3": "ORR"})
    second = _write("b.yaml", "bob", {"u1": "PFS", "u2": "PFS", "u4": "ORR"})

    result = agreement(first, second)
    assert result["items_shared"] == 2
    assert result["only_in_first"] == ["u3"]
    assert result["only_in_second"] == ["u4"]
    assert result["percent_agreement"] == 50.0
    assert len(result["disagreements"]) == 1
    assert result["disagreements"][0]["outcome_uid"] == "u2"


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(writable_db, monkeypatch):
    """A test client pointed at a scratch database, with writes off by default."""
    import dataclasses

    import ceskb.api.app as api_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        api_app, "PATHS", dataclasses.replace(api_app.PATHS, database=writable_db)
    )
    monkeypatch.delenv(api_app.ALLOW_REVIEW_ENV, raising=False)
    return TestClient(api_app.app)


def test_review_endpoints_report_the_queue_and_decisions(client):
    payload = client.get("/api/review/queue").json()
    assert {"queue", "by_reason", "decisions"} <= set(payload)
    assert client.get("/api/review/overrides").json() == []
    assert client.get("/api/review/evaluations").json() == []


def test_review_writes_are_refused_by_default(client):
    """The API is read-only unless somebody deliberately opened it."""
    response = client.post(
        "/api/review/overrides",
        json={
            "outcome_uid": "SYNTH-0001:primary:0",
            "concept_id": "ORR",
            "reason": "A reason long enough to satisfy the schema.",
            "reviewer": "tester",
        },
    )
    assert response.status_code == 403
    assert "--allow-review" in response.json()["detail"]
    assert client.get("/healthz").json()["review_writes"] == "false"


def test_review_writes_work_when_deliberately_enabled(client, monkeypatch, tmp_path):
    import ceskb.api.app as api_app
    import ceskb.review.overrides as overrides_module

    monkeypatch.setenv(api_app.ALLOW_REVIEW_ENV, "1")
    monkeypatch.setattr(
        overrides_module,
        "PATHS",
        __import__("dataclasses").replace(
            overrides_module.PATHS, overrides=tmp_path / "overrides.yaml"
        ),
    )
    response = client.post(
        "/api/review/overrides",
        json={
            "outcome_uid": "SYNTH-0021:secondary:1",
            "concept_id": "ORR",
            "reason": "Reviewed against the protocol: objective response rate.",
            "reviewer": "tester",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["recorded"]["concept_id"] == "ORR"
    assert (tmp_path / "overrides.yaml").exists()


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"reviewer": "t", "reason": "long enough reason"}, "outcome_uid"),
        ({"outcome_uid": "SYNTH-0001:primary:0", "reviewer": "t", "reason": "short"}, "8 characters"),
        (
            {
                "outcome_uid": "SYNTH-0001:primary:0",
                "reviewer": "t",
                "reason": "A reason long enough to satisfy the schema.",
                "concept_id": "NOT_A_CONCEPT",
            },
            "unknown concept",
        ),
        (
            {
                "outcome_uid": "SYNTH-0001:primary:0",
                "reviewer": "t",
                "reason": "A reason long enough to satisfy the schema.",
            },
            "nothing to record",
        ),
    ],
)
def test_bad_review_writes_are_rejected_before_anything_is_written(
    client, monkeypatch, tmp_path, body, expected
):
    import ceskb.api.app as api_app
    import ceskb.review.overrides as overrides_module

    target = tmp_path / "overrides.yaml"
    monkeypatch.setenv(api_app.ALLOW_REVIEW_ENV, "1")
    monkeypatch.setattr(
        overrides_module,
        "PATHS",
        __import__("dataclasses").replace(overrides_module.PATHS, overrides=target),
    )
    response = client.post("/api/review/overrides", json=body)
    assert response.status_code == 400
    assert expected in response.json()["detail"]
    assert not target.exists(), "a rejected decision must leave nothing on disk"


def test_agreement_counts_a_shared_no_match_as_agreement(tmp_path):
    """Deciding nothing fits is a judgement, not a missing answer."""

    def _write(name, annotator):
        path = tmp_path / name
        path.write_text(
            yaml.safe_dump(
                {
                    "gold_set_id": name,
                    "annotator": annotator,
                    "independence": "independent",
                    "items": [
                        {"outcome_uid": "u1", "concept_id": None},
                        {"outcome_uid": "u2", "concept_id": "PFS"},
                    ],
                }
            )
        )
        return load_gold_set(path)

    result = agreement(_write("a.yaml", "alice"), _write("b.yaml", "bob"))
    assert result["percent_agreement"] == 100.0
    assert result["disagreements"] == []
