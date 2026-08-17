"""Classification behaviour, expressed as the cases that matter clinically.

The parametrised table doubles as documentation: it is the record of which registry
phrasings are known to map where, and it is the first place to add a case when a
misclassification is reported.
"""

from __future__ import annotations

import pytest

from ceskb.classify.engine import DEFINING_AXES, OutcomeRecord, classify_outcome
from ceskb.classify.extractors import (
    TextFields,
    extract_analysis_population,
    extract_threshold,
    extract_timepoint_anchor,
    extract_timepoint_offset,
)


def _classify(vocab, measure, description="", time_frame="", tas=(), level="primary"):
    return classify_outcome(
        OutcomeRecord(
            outcome_uid="T:primary:0",
            study_id="T",
            endpoint_level=level,
            measure=measure,
            description=description,
            time_frame=time_frame,
            therapeutic_areas=tuple(tas),
        ),
        vocab,
    )


@pytest.mark.parametrize(
    "measure,expected",
    [
        ("Progression-Free Survival (PFS) per RECIST v1.1", "PFS"),
        ("Overall Survival (OS)", "OS"),
        ("Objective Response Rate (ORR)", "ORR"),
        ("Time to Disease Progression", "TTP"),
        ("Duration of Response (DoR)", "DOR"),
        ("Pathological Complete Response (pCR) Rate", "PCR_RATE"),
        ("Disease-Free Survival", "DFS"),
        ("Change From Baseline in Trough FEV1 at Week 24", "FEV1_CFB"),
        ("Absolute Change From Baseline in Percent Predicted FEV1", "FEV1_PCT_PREDICTED"),
        ("Annualized Rate of Moderate or Severe COPD Exacerbations", "EXACERBATION_RATE"),
        ("Time to First Moderate or Severe Exacerbation", "TIME_TO_FIRST_EXACERBATION"),
        ("Change From Baseline in HbA1c at Week 26", "HBA1C_CFB"),
        ("Percent Change From Baseline in Body Weight at Week 68", "BODY_WEIGHT_PCT_CFB"),
        ("Change From Baseline in Body Weight at Week 26", "BODY_WEIGHT_CFB"),
        ("Percentage of Time in Target Glucose Range", "CGM_TIME_IN_RANGE"),
        ("Percentage of Participants Achieving an ACR20 Response at Week 12", "ACR20"),
        ("Percentage of Participants Achieving PASI 90 Response at Week 16", "PASI90"),
        ("Percentage of Participants Achieving EASI-75 at Week 16", "EASI75"),
        ("Time to First Occurrence of Major Adverse Cardiovascular Events (MACE)", "MACE_TIME_TO_FIRST"),
        ("Chronic eGFR Slope From Week 12 to End of Treatment", "EGFR_SLOPE"),
        ("Annualized Relapse Rate (ARR) at Week 96", "ANNUALISED_RELAPSE_RATE"),
        ("Change From Baseline in MADRS Total Score at Week 6", "DEPRESSION_SCALE_CFB"),
        ("Change From Baseline in Best-Corrected Visual Acuity (BCVA) at Week 48", "BCVA_CFB"),
        ("Number of Participants With Treatment-Emergent Adverse Events (TEAEs)", "AE_INCIDENCE"),
    ],
)
def test_known_phrasings_map_to_expected_concepts(vocab, measure, expected):
    spec = _classify(vocab, measure)
    assert spec is not None, f"no rule fired for {measure!r}"
    assert spec.concept_id == expected


def test_landmark_rate_is_not_read_as_time_to_event(vocab):
    """A PFS rate at 12 months is a proportion. Conflating the two misstates the result."""
    spec = _classify(vocab, "Progression-Free Survival Rate at 12 Months")
    assert spec is not None
    assert spec.concept_id == "PFS"
    assert spec.axes["endpoint_form"][0] == "responder_binary"
    assert spec.axes["endpoint_form"][1] == "rule_assert"
    assert spec.axes["scale_type"][0] == "proportion"


def test_time_to_event_selection_is_not_overridden_by_time_frame_prose(vocab):
    spec = _classify(vocab, "Progression-Free Survival", time_frame="Up to approximately 36 months")
    assert spec.axes["timepoint_selection"][0] == "first_occurrence"
    assert spec.axes["timepoint_selection"][1] == "concept_default"


def test_unmatchable_text_is_left_unclassified(vocab):
    assert _classify(vocab, "Investigator-Assessed Clinical Benefit") is None
    assert _classify(vocab, "Exploratory Biomarker Analyses") is None


def test_extractors_never_redefine_a_defining_axis(vocab):
    """The structural guarantee that makes Layer A trustworthy."""
    spec = _classify(
        vocab,
        "Change From Baseline in HbA1c at Week 26",
        description="Time to event analysis of best overall response with at least a 30% reduction",
        time_frame="From randomization up to 36 months",
    )
    assert spec is not None
    for axis in DEFINING_AXES:
        if axis in spec.axes:
            assert spec.axes[axis][1] in {"concept_default", "rule_assert"}, (
                f"{axis} was set by an extractor"
            )


def test_composite_criteria_threshold_is_not_overwritten_by_extraction(vocab):
    """ACR20 means a seven-component criteria set, not "20 percent of something"."""
    spec = _classify(
        vocab,
        "Percentage of Participants Achieving an ACR20 Response at Week 12",
        description="At least a 20% improvement in tender and swollen joint counts.",
    )
    assert spec.axes["threshold_kind"][0] == "composite_criteria"
    assert spec.axes["threshold_kind"][1] == "concept_default"


def test_convention_threshold_yields_to_the_studys_own_value(vocab):
    """The concept's 5% is a convention; a protocol saying 10% must win."""
    spec = _classify(
        vocab,
        "Percentage of Participants Achieving at Least 10% Reduction in Body Weight",
        description="Participants with at least a 10% decrease in body weight from baseline.",
    )
    assert spec.concept_id == "WEIGHT_LOSS_RESPONDER"
    assert spec.threshold_value == 10.0
    assert spec.axes["threshold_kind"][1] == "extracted"


def test_competing_rules_are_all_recorded(vocab):
    spec = _classify(vocab, "Progression-Free Survival Rate at 12 Months")
    assert spec is not None
    fired = {m.rule.rule_id for m in spec.rule_matches}
    assert "pfs.landmark_rate" in fired
    assert sum(1 for m in spec.rule_matches if m.selected) == 1


def test_none_of_guard_blocks_on_the_fields_a_rule_declares(vocab):
    """A rule reading measure_and_description must be disqualified by either half.

    Rules that declare only `measure` are deliberately not affected by description
    text: an outcome titled "Objective Response Rate" is ORR whatever its description
    elaborates on.
    """
    spec = _classify(
        vocab,
        "Percentage of Participants With Complete Response or Partial Response",
        description="Duration of response among responders.",
        tas=("oncology",),
    )
    assert spec is None or spec.concept_id != "ORR"

    # Without the disqualifier the same title does classify.
    ok = _classify(
        vocab,
        "Percentage of Participants With Complete Response or Partial Response",
        description="Assessed by investigator per RECIST v1.1.",
        tas=("oncology",),
    )
    assert ok is not None and ok.concept_id == "ORR"


# --------------------------------------------------------------------------- #
# extractors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "time_frame,anchor",
    [
        ("From randomization until death, up to 60 months", "randomisation"),
        ("From first dose up to 30 days after last dose", "first_dose"),
        ("Baseline and Week 24", "baseline_visit"),
        ("At Week 16", "unspecified"),
        ("From screening to end of study", "screening"),
    ],
)
def test_timepoint_anchor_extraction(time_frame, anchor):
    result = extract_timepoint_anchor(TextFields("", "", time_frame))
    assert result.term_id == anchor


@pytest.mark.parametrize(
    "time_frame,value,unit",
    [
        ("At Week 12", 12.0, "week"),
        ("Baseline and Week 26", 26.0, "week"),
        ("Up to 36 months", 36.0, "month"),
        ("At Day 28", 28.0, "day"),
        ("Through 2 years", 2.0, "year"),
    ],
)
def test_timepoint_offset_extraction(time_frame, value, unit):
    result = extract_timepoint_offset(TextFields("", "", time_frame))
    assert result is not None
    assert result.value_num == value
    assert result.unit == unit


def test_timepoint_offset_absent_when_no_number():
    assert extract_timepoint_offset(TextFields("", "", "Until disease progression")) is None


@pytest.mark.parametrize(
    "text,operator,value",
    [
        ("At least a 20% improvement in joint counts", "increase_by_at_least", 20.0),
        ("A 30% decrease in the sum of diameters", "decrease_by_at_least", 30.0),
        ("Reduction of at least 75%", "decrease_by_at_least", 75.0),
    ],
)
def test_threshold_extraction(text, operator, value):
    results = extract_threshold(TextFields(text, "", ""))
    assert results
    operators = {r.term_id for r in results if r.axis_id == "threshold_operator"}
    assert operator in operators
    assert results[0].value_num == value


@pytest.mark.parametrize(
    "text,population",
    [
        ("Intent-to-treat population", "itt"),
        ("Modified intent-to-treat population", "modified_itt"),
        ("Full analysis set", "full_analysis_set"),
        ("Safety population; all treated participants", "safety"),
        ("Per-protocol set", "per_protocol"),
        ("No population stated", "unspecified"),
    ],
)
def test_analysis_population_extraction(text, population):
    assert extract_analysis_population(TextFields("", text, "")).term_id == population
