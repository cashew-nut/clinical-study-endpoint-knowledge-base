"""Generate the seed gold set from hand-written annotations.

The judgements below were made by reading the outcome text -- measure, description and
time frame -- and deciding what the endpoint is, without consulting classifier output.
Everything mechanical (the record hash that binds an annotation to its text, the copy
of the measure) is filled in from the database, because transcribing 56 hashes by hand
is a good way to produce a gold set that silently scores nothing.

The set is nonetheless marked `self_annotated`: the same party wrote the rules and
these answers, so a misconception shared between them is invisible here. That is why
`independence` is a required field and why the scorer prints a caveat.

Run:  python scripts/make_gold_seed.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]

# outcome_uid -> (concept_id | None, axes, difficulty, note)
#
# difficulty is the annotator's own read of the call:
#   clear      the text names a standard endpoint and one concept plainly fits
#   judgement  a defensible alternative reading exists
#   ambiguous  the text does not settle it; recorded so these are scored separately
ANNOTATIONS: dict[str, tuple[str | None, dict[str, str], str, str]] = {
    # -- oncology ----------------------------------------------------------------
    "SYNTH-0001:primary:0": (
        "PFS",
        {"endpoint_form": "time_to_event", "reference_type": "randomisation_time",
         "timepoint_anchor": "randomisation"},
        "clear",
        "Names PFS and describes time from randomisation to progression or death.",
    ),
    "SYNTH-0001:primary:1": (
        "OS",
        {"endpoint_form": "time_to_event", "reference_type": "randomisation_time"},
        "clear",
        "",
    ),
    "SYNTH-0001:secondary:0": (
        "ORR",
        {"endpoint_form": "responder_binary", "scale_type": "proportion"},
        "clear",
        "Best overall response of confirmed CR or PR is the RECIST definition of ORR.",
    ),
    "SYNTH-0001:secondary:1": ("DOR", {}, "clear", ""),
    "SYNTH-0001:secondary:2": (
        "AE_INCIDENCE",
        {"analysis_population": "safety"},
        "clear",
        "Description states the safety population explicitly.",
    ),
    "SYNTH-0001:exploratory:0": (
        "HRQOL_CFB",
        {"endpoint_form": "change_from_baseline", "reference_type": "patient_baseline"},
        "clear",
        "",
    ),
    "SYNTH-0002:primary:0": ("PCR_RATE", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0002:secondary:0": ("EFS", {"endpoint_form": "time_to_event"}, "clear", ""),
    "SYNTH-0002:secondary:1": (
        "ORR",
        {"endpoint_form": "responder_binary"},
        "judgement",
        "Spells out CR-or-PR without naming ORR. The union of CR and PR is ORR, not "
        "CR rate -- a classifier keying on 'Complete Response' alone would get this wrong.",
    ),
    "SYNTH-0003:primary:0": ("PFS", {"endpoint_form": "time_to_event"}, "clear", ""),
    "SYNTH-0003:secondary:0": ("MRD_NEGATIVITY_RATE", {"endpoint_form": "responder_binary"},
                               "clear", ""),
    "SYNTH-0003:secondary:1": ("CR_RATE", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0021:primary:0": (
        "PFS",
        {"endpoint_form": "responder_binary", "scale_type": "proportion"},
        "judgement",
        "A landmark rate, not a time-to-event analysis: the derived variable is the "
        "proportion progression-free at a fixed 12 months. Same measurement concept as "
        "PFS, different form -- which is exactly what the form axis is for.",
    ),
    "SYNTH-0021:secondary:0": (
        "OS",
        {"endpoint_form": "responder_binary", "scale_type": "proportion"},
        "judgement",
        "Landmark survival rate, as above.",
    ),
    "SYNTH-0021:secondary:1": (
        None,
        {},
        "ambiguous",
        "'Investigator-Assessed Clinical Benefit', explicitly not otherwise specified. "
        "No concept can be assigned from this text, and inventing one would be worse "
        "than leaving it unmatched.",
    ),
    # -- respiratory --------------------------------------------------------------
    "SYNTH-0004:primary:0": (
        "FEV1_CFB",
        {"endpoint_form": "change_from_baseline", "reference_type": "patient_baseline",
         "analysis_population": "full_analysis_set"},
        "clear",
        "",
    ),
    "SYNTH-0004:primary:1": (
        "EXACERBATION_RATE",
        {"endpoint_form": "event_rate", "scale_type": "rate"},
        "clear",
        "Annualised rate per participant-year.",
    ),
    "SYNTH-0004:secondary:0": (
        "TIME_TO_FIRST_EXACERBATION",
        {"endpoint_form": "time_to_event", "timepoint_selection": "first_occurrence"},
        "clear",
        "",
    ),
    "SYNTH-0004:secondary:1": (
        "HRQOL_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "decrease_is_better"},
        "judgement",
        "SGRQ scores higher for worse health, so a fall is improvement -- the opposite "
        "of the generic quality-of-life convention. Annotated deliberately to test "
        "whether instrument-specific direction survives.",
    ),
    "SYNTH-0005:primary:0": (
        "FEV1_PCT_PREDICTED",
        {"endpoint_form": "change_from_baseline", "reference_type": "patient_baseline"},
        "judgement",
        "Two references are in play. 'Percent predicted' references the measurement to a "
        "population norm; the endpoint then compares that quantity to the participant's "
        "own baseline. The reference_type axis describes the endpoint's comparison, so "
        "it is patient_baseline, and the population norm is a property of how the "
        "measurement is expressed. Annotated population_normative on the first pass by "
        "carrying over the concept's framing without separating the two.",
    ),
    "SYNTH-0005:secondary:0": (
        "EXACERBATION_RATE",
        {"endpoint_form": "count"},
        "judgement",
        "A count over a fixed window, not an annualised rate. Same measurement, "
        "different form.",
    ),
    # -- metabolic ----------------------------------------------------------------
    "SYNTH-0006:primary:0": (
        "HBA1C_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "decrease_is_better",
         "analysis_population": "full_analysis_set"},
        "clear",
        "",
    ),
    "SYNTH-0006:secondary:0": (
        "HBA1C_TARGET_RESPONDER",
        {"endpoint_form": "responder_binary", "reference_type": "fixed_clinical_target"},
        "clear",
        "Referenced to an absolute target of 7.0%, not to the patient's own baseline.",
    ),
    "SYNTH-0006:secondary:1": (
        "BODY_WEIGHT_CFB",
        {"endpoint_form": "change_from_baseline", "scale_type": "continuous"},
        "clear",
        "Absolute change, so not the percent-change concept.",
    ),
    "SYNTH-0007:primary:0": (
        "BODY_WEIGHT_PCT_CFB",
        {"endpoint_form": "percent_change_from_baseline", "direction": "decrease_is_better"},
        "clear",
        "",
    ),
    "SYNTH-0007:secondary:0": (
        "WEIGHT_LOSS_RESPONDER",
        {"endpoint_form": "responder_binary", "threshold_kind": "relative_change_percent"},
        "clear",
        "",
    ),
    "SYNTH-0007:secondary:1": (
        "WEIGHT_LOSS_RESPONDER",
        {"endpoint_form": "responder_binary", "threshold_kind": "relative_change_percent"},
        "clear",
        "Same concept as the 5% item, differing only in threshold value -- which is a "
        "Layer B parameter, not a second concept.",
    ),
    "SYNTH-0008:primary:0": ("CGM_TIME_IN_RANGE", {"direction": "increase_is_better"},
                             "clear", ""),
    "SYNTH-0008:secondary:0": ("HBA1C_CFB", {"endpoint_form": "change_from_baseline"},
                               "clear", ""),
    # -- immune-mediated ----------------------------------------------------------
    "SYNTH-0009:primary:0": (
        "ACR20",
        {"endpoint_form": "responder_binary", "analysis_population": "itt"},
        "clear",
        "",
    ),
    "SYNTH-0009:secondary:0": ("ACR50", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0009:secondary:1": ("ACR70", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0009:secondary:2": (
        "DAS28_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "decrease_is_better"},
        "clear",
        "",
    ),
    "SYNTH-0010:primary:0": ("PASI90", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0010:secondary:0": ("PASI75", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0010:exploratory:0": ("AE_INCIDENCE", {}, "clear", ""),
    "SYNTH-0011:primary:0": ("EASI75", {"endpoint_form": "responder_binary"}, "clear", ""),
    "SYNTH-0011:secondary:0": (
        "PAIN_INTENSITY_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "decrease_is_better"},
        "clear",
        "",
    ),
    # -- cardiometabolic, renal, neuro --------------------------------------------
    "SYNTH-0012:primary:0": (
        "MACE_TIME_TO_FIRST",
        {"endpoint_form": "time_to_event", "timepoint_selection": "first_occurrence"},
        "clear",
        "",
    ),
    "SYNTH-0012:secondary:0": (
        "HF_HOSPITALISATION_RECURRENT",
        {"direction": "decrease_is_better"},
        "clear",
        "Recurrent-event analysis of all hospitalisations, not time to the first.",
    ),
    "SYNTH-0012:secondary:1": ("SBP_CFB", {"endpoint_form": "change_from_baseline"},
                               "clear", ""),
    "SYNTH-0012:secondary:2": (
        "SIX_MWD_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "increase_is_better"},
        "clear",
        "",
    ),
    "SYNTH-0013:primary:0": (
        "EGFR_SLOPE",
        {"endpoint_form": "slope_over_time", "direction": "increase_is_better"},
        "judgement",
        "Slope of eGFR over time. Direction: a less negative slope is better, which the "
        "vocabulary expresses as increase_is_better on the slope itself.",
    ),
    "SYNTH-0013:secondary:0": (
        "UACR_PCT_CFB",
        {"endpoint_form": "percent_change_from_baseline", "direction": "decrease_is_better"},
        "clear",
        "",
    ),
    "SYNTH-0014:primary:0": (
        "ANNUALISED_RELAPSE_RATE",
        {"endpoint_form": "event_rate", "scale_type": "rate"},
        "clear",
        "",
    ),
    "SYNTH-0014:secondary:0": (
        "CONFIRMED_DISABILITY_PROGRESSION",
        {"endpoint_form": "time_to_event"},
        "clear",
        "",
    ),
    "SYNTH-0015:primary:0": ("ADAS_COG_CFB", {"endpoint_form": "change_from_baseline"},
                             "clear", ""),
    "SYNTH-0015:secondary:0": ("HRQOL_CFB", {"endpoint_form": "change_from_baseline"},
                               "clear", ""),
    "SYNTH-0016:primary:0": (
        "DEPRESSION_SCALE_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "decrease_is_better"},
        "clear",
        "",
    ),
    "SYNTH-0016:secondary:0": ("AE_INCIDENCE", {}, "clear", ""),
    "SYNTH-0017:primary:0": (
        "SEIZURE_FREQUENCY_PCT_CFB",
        {"endpoint_form": "percent_change_from_baseline", "direction": "decrease_is_better"},
        "clear",
        "",
    ),
    # -- infectious disease, ophthalmology ----------------------------------------
    "SYNTH-0018:primary:0": (
        "VIRAL_SUPPRESSION_RATE",
        {"endpoint_form": "responder_binary", "reference_type": "fixed_clinical_target"},
        "clear",
        "",
    ),
    "SYNTH-0018:secondary:0": ("AE_INCIDENCE", {}, "clear", ""),
    "SYNTH-0019:primary:0": (
        "SEROCONVERSION_RATE",
        {"endpoint_form": "responder_binary", "reference_type": "patient_baseline"},
        "clear",
        "Four-fold rise is referenced to the participant's own pre-vaccination titre.",
    ),
    "SYNTH-0020:primary:0": (
        "BCVA_CFB",
        {"endpoint_form": "change_from_baseline", "direction": "increase_is_better"},
        "clear",
        "",
    ),
    "SYNTH-0020:secondary:0": (
        "BCVA_LETTER_GAIN_RESPONDER",
        {"endpoint_form": "responder_binary", "threshold_kind": "absolute_change"},
        "clear",
        "",
    ),
}


def main() -> int:
    db = ROOT / "data" / "ceskb.duckdb"
    if not db.exists():
        print(f"no database at {db}; run `ceskb refresh --source fixtures` first")
        return 1

    conn = duckdb.connect(str(db), read_only=True)
    rows = {
        uid: (measure, record_hash)
        for uid, measure, record_hash in conn.execute(
            "SELECT outcome_uid, measure, record_hash FROM study_outcome"
        ).fetchall()
    }
    conn.close()

    missing = sorted(set(ANNOTATIONS) - set(rows))
    if missing:
        print("annotations reference outcomes that are not in the database:")
        for uid in missing:
            print(f"  {uid}")
        return 1

    unannotated = sorted(set(rows) - set(ANNOTATIONS))
    if unannotated:
        print(f"note: {len(unannotated)} outcome(s) in the database are not annotated")

    items = []
    for uid in sorted(ANNOTATIONS):
        concept_id, axes, difficulty, note = ANNOTATIONS[uid]
        measure, record_hash = rows[uid]
        item: dict[str, object] = {
            "outcome_uid": uid,
            "concept_id": concept_id,
            "text": measure,
            "source_hash": record_hash,
        }
        if axes:
            item["axes"] = axes
        item["difficulty"] = difficulty
        if note:
            item["note"] = note
        items.append(item)

    payload = {
        "gold_set_id": "fixtures-seed",
        "version": "1",
        "description": (
            "Seed annotations over the synthetic fixture corpus. Judgements were made "
            "from the outcome text alone. Because the same party authored the rules "
            "and these answers, the set measures self-consistency and regression, not "
            "accuracy -- see independence."
        ),
        "annotator": "kb-author",
        "annotated_at": "2026-08-17",
        "guideline": "docs/ANNOTATION.md",
        "independence": "self_annotated",
        "items": items,
    }

    out = ROOT / "review" / "gold" / "fixtures-seed.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100))
    print(f"wrote {out} with {len(items)} annotations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
