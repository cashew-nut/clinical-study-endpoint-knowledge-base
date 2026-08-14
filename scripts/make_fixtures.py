#!/usr/bin/env python3
"""Generate the synthetic fixture corpus.

Kept as a script rather than a static blob so the phrasing patterns being tested are
readable as a list, and so a new phrasing variant can be added in one line.

The outcome titles imitate ClinicalTrials.gov conventions. Everything else -- the
identifiers, sponsors, conditions, dates -- is invented, and identifiers use a
SYNTH- prefix so they cannot collide with real NCT numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "fixtures" / "synthetic_corpus.json"

# (conditions, phases, sponsor, [(level, measure, description, time_frame), ...])
STUDIES: list[tuple[list[str], list[str], str, list[tuple[str, str, str, str]]]] = [
    (
        ["Non-Small Cell Lung Cancer"],
        ["PHASE3"],
        "Example Oncology Sponsor A",
        [
            ("primary", "Progression-Free Survival (PFS) as Assessed by Blinded Independent Central Review per RECIST v1.1",
             "Time from randomization to first documented disease progression or death from any cause.",
             "From randomization until disease progression or death, up to approximately 36 months"),
            ("primary", "Overall Survival (OS)",
             "Time from randomization to death from any cause.",
             "From randomization until death, up to approximately 60 months"),
            ("secondary", "Objective Response Rate (ORR) per RECIST v1.1",
             "Percentage of participants with a best overall response of confirmed complete response or partial response.",
             "From randomization up to approximately 36 months"),
            ("secondary", "Duration of Response (DoR)", "", "Up to approximately 36 months"),
            ("secondary", "Number of Participants With Treatment-Emergent Adverse Events (TEAEs)",
             "Safety population. All participants who received at least one dose.",
             "From first dose up to 30 days after last dose"),
            ("exploratory", "Change From Baseline in Quality of Life Score at Week 12",
             "Exploratory patient-reported outcome analysis.", "Baseline and Week 12"),
        ],
    ),
    (
        ["Breast Cancer"],
        ["PHASE3"],
        "Example Oncology Sponsor B",
        [
            ("primary", "Pathological Complete Response (pCR) Rate",
             "Percentage of participants with no residual invasive disease at definitive surgery.",
             "At definitive surgery, approximately Week 24"),
            ("secondary", "Event-Free Survival (EFS)", "", "Up to 60 months"),
            ("secondary", "Percentage of Participants With Complete Response or Partial Response",
             "Assessed by investigator per RECIST v1.1.", "Up to 24 months"),
        ],
    ),
    (
        ["Multiple Myeloma"],
        ["PHASE3"],
        "Example Haematology Sponsor",
        [
            ("primary", "Progression-Free Survival", "", "Up to approximately 48 months"),
            ("secondary", "MRD-Negativity Rate",
             "Percentage of participants achieving minimal residual disease negativity at a sensitivity of 10^-5 by next-generation sequencing.",
             "At Month 12"),
            ("secondary", "Complete Response Rate", "", "Up to 48 months"),
        ],
    ),
    (
        ["Chronic Obstructive Pulmonary Disease"],
        ["PHASE3"],
        "Example Respiratory Sponsor",
        [
            ("primary", "Change From Baseline in Trough FEV1 at Week 24",
             "Trough forced expiratory volume in 1 second measured by spirometry. Full analysis set.",
             "Baseline and Week 24"),
            ("primary", "Annualized Rate of Moderate or Severe COPD Exacerbations",
             "Rate of exacerbations per participant-year over the treatment period.",
             "From randomization to Week 52"),
            ("secondary", "Time to First Moderate or Severe Exacerbation", "", "From randomization up to Week 52"),
            ("secondary", "Change From Baseline in St George's Respiratory Questionnaire Total Score at Week 24",
             "", "Baseline and Week 24"),
        ],
    ),
    (
        ["Cystic Fibrosis"],
        ["PHASE3"],
        "Example Rare Disease Sponsor",
        [
            ("primary", "Absolute Change From Baseline in Percent Predicted FEV1 (ppFEV1) at Week 24",
             "", "Baseline through Week 24"),
            ("secondary", "Number of Pulmonary Exacerbations", "", "Through Week 24"),
        ],
    ),
    (
        ["Type 2 Diabetes Mellitus"],
        ["PHASE3"],
        "Example Metabolic Sponsor A",
        [
            ("primary", "Change From Baseline in HbA1c at Week 26",
             "Change from baseline in glycated hemoglobin. Full analysis set, MMRM.",
             "Baseline, Week 26"),
            ("secondary", "Percentage of Participants Achieving HbA1c < 7.0%",
             "Proportion of participants with HbA1c below 7.0 percent at Week 26.",
             "At Week 26"),
            ("secondary", "Change From Baseline in Body Weight at Week 26", "", "Baseline, Week 26"),
        ],
    ),
    (
        ["Obesity", "Overweight"],
        ["PHASE3"],
        "Example Metabolic Sponsor B",
        [
            ("primary", "Percent Change From Baseline in Body Weight at Week 68",
             "", "Baseline to Week 68"),
            ("secondary", "Percentage of Participants Achieving at Least 5% Reduction in Body Weight",
             "Percentage of participants with at least a 5% decrease in body weight from baseline.",
             "At Week 68"),
            ("secondary", "Percentage of Participants Achieving at Least 10% Reduction in Body Weight",
             "", "At Week 68"),
        ],
    ),
    (
        ["Type 1 Diabetes Mellitus"],
        ["PHASE2"],
        "Example Device Sponsor",
        [
            ("primary", "Percentage of Time in Target Glucose Range (70-180 mg/dL)",
             "Measured by continuous glucose monitoring over the 12-week treatment period.",
             "Weeks 1 through 12"),
            ("secondary", "Change From Baseline in HbA1c at Week 12", "", "Baseline and Week 12"),
        ],
    ),
    (
        ["Rheumatoid Arthritis"],
        ["PHASE3"],
        "Example Immunology Sponsor A",
        [
            ("primary", "Percentage of Participants Achieving an ACR20 Response at Week 12",
             "American College of Rheumatology 20% response. Intent-to-treat population; non-responder imputation.",
             "At Week 12"),
            ("secondary", "Percentage of Participants Achieving an ACR50 Response at Week 24", "", "At Week 24"),
            ("secondary", "Percentage of Participants Achieving an ACR70 Response at Week 24", "", "At Week 24"),
            ("secondary", "Change From Baseline in DAS28-CRP at Week 24", "", "Baseline and Week 24"),
        ],
    ),
    (
        ["Plaque Psoriasis"],
        ["PHASE3"],
        "Example Dermatology Sponsor",
        [
            ("primary", "Percentage of Participants Achieving PASI 90 Response at Week 16",
             "At least a 90% improvement in Psoriasis Area and Severity Index from baseline.",
             "At Week 16"),
            ("secondary", "Percentage of Participants Achieving PASI 75 Response at Week 16", "", "At Week 16"),
            ("exploratory", "Number of Participants With Adverse Events of Special Interest",
             "Exploratory safety analysis.", "Up to Week 52"),
        ],
    ),
    (
        ["Atopic Dermatitis"],
        ["PHASE3"],
        "Example Immunology Sponsor B",
        [
            ("primary", "Percentage of Participants Achieving EASI-75 at Week 16",
             "At least a 75% reduction in Eczema Area and Severity Index from baseline.",
             "At Week 16"),
            ("secondary", "Change From Baseline in Pain Intensity NRS at Week 16", "", "Baseline and Week 16"),
        ],
    ),
    (
        ["Heart Failure With Reduced Ejection Fraction"],
        ["PHASE3"],
        "Example Cardiovascular Sponsor",
        [
            ("primary", "Time to First Occurrence of Major Adverse Cardiovascular Events (MACE)",
             "Composite of cardiovascular death, non-fatal myocardial infarction, or non-fatal stroke, adjudicated by an independent clinical events committee.",
             "From randomization up to 48 months"),
            ("secondary", "Total Number of Heart Failure Hospitalizations",
             "Recurrent event analysis of all heart failure hospitalizations.",
             "From randomization up to 48 months"),
            ("secondary", "Change From Baseline in Systolic Blood Pressure at Week 12", "", "Baseline and Week 12"),
            ("secondary", "Change From Baseline in 6-Minute Walk Distance at Week 24", "", "Baseline and Week 24"),
        ],
    ),
    (
        ["Chronic Kidney Disease", "Diabetic Nephropathy"],
        ["PHASE3"],
        "Example Nephrology Sponsor",
        [
            ("primary", "Chronic eGFR Slope From Week 12 to End of Treatment",
             "Annual rate of change in estimated glomerular filtration rate.",
             "Week 12 to Week 104"),
            ("secondary", "Percent Change From Baseline in Urine Albumin-to-Creatinine Ratio (UACR) at Week 24",
             "", "Baseline and Week 24"),
        ],
    ),
    (
        ["Relapsing-Remitting Multiple Sclerosis"],
        ["PHASE3"],
        "Example Neurology Sponsor A",
        [
            ("primary", "Annualized Relapse Rate (ARR) at Week 96",
             "Number of confirmed relapses per participant-year.",
             "From randomization to Week 96"),
            ("secondary", "Time to 12-Week Confirmed Disability Progression",
             "Increase of at least 1.0 point in EDSS confirmed at 12 weeks.",
             "Up to Week 96"),
        ],
    ),
    (
        ["Alzheimer Disease"],
        ["PHASE3"],
        "Example Neurology Sponsor B",
        [
            ("primary", "Change From Baseline in ADAS-Cog 13 at Week 78", "", "Baseline and Week 78"),
            ("secondary", "Change From Baseline in Quality of Life Score at Week 78", "", "Baseline and Week 78"),
        ],
    ),
    (
        ["Major Depressive Disorder"],
        ["PHASE3"],
        "Example Psychiatry Sponsor",
        [
            ("primary", "Change From Baseline in MADRS Total Score at Week 6",
             "Montgomery-Asberg Depression Rating Scale. MMRM, modified intent-to-treat population.",
             "Baseline and Week 6"),
            ("secondary", "Percentage of Participants With Adverse Events", "", "Up to Week 8"),
        ],
    ),
    (
        ["Focal Epilepsy"],
        ["PHASE3"],
        "Example CNS Sponsor",
        [
            ("primary", "Percent Change From Baseline in Seizure Frequency During the Maintenance Period",
             "", "Baseline period through Week 18"),
        ],
    ),
    (
        ["HIV-1 Infection"],
        ["PHASE3"],
        "Example Infectious Disease Sponsor",
        [
            ("primary", "Percentage of Participants With HIV-1 RNA < 50 Copies/mL at Week 48",
             "Virologic suppression. FDA snapshot algorithm, intent-to-treat exposed population.",
             "At Week 48"),
            ("secondary", "Number of Participants With Treatment-Emergent Adverse Events", "", "Up to Week 96"),
        ],
    ),
    (
        ["Influenza"],
        ["PHASE3"],
        "Example Vaccine Sponsor",
        [
            ("primary", "Seroconversion Rate at Day 28",
             "Percentage of participants achieving a four-fold or greater rise in haemagglutination inhibition titre from baseline.",
             "Baseline and Day 28"),
        ],
    ),
    (
        ["Neovascular Age-Related Macular Degeneration"],
        ["PHASE3"],
        "Example Ophthalmology Sponsor",
        [
            ("primary", "Change From Baseline in Best-Corrected Visual Acuity (BCVA) at Week 48",
             "Measured in ETDRS letters.", "Baseline and Week 48"),
            ("secondary", "Percentage of Participants Gaining at Least 15 Letters in BCVA at Week 48",
             "", "At Week 48"),
        ],
    ),
    (
        ["Advanced Melanoma"],
        ["PHASE2"],
        "Example Oncology Sponsor C",
        [
            # Deliberate hard cases: a landmark rate that must not be read as
            # time-to-event, and a title with no resolvable timepoint anchor.
            ("primary", "Progression-Free Survival Rate at 12 Months",
             "Percentage of participants alive and progression-free at 12 months.",
             "At 12 months"),
            ("secondary", "Overall Survival Rate at 24 Months", "", "At 24 months"),
            ("secondary", "Investigator-Assessed Clinical Benefit", "Not otherwise specified.", "Up to 24 months"),
        ],
    ),
]


def build() -> dict:
    studies = []
    for index, (conditions, phases, sponsor, outcomes) in enumerate(STUDIES, start=1):
        study_id = f"SYNTH-{index:04d}"
        grouped: dict[str, list[dict[str, str]]] = {
            "primaryOutcomes": [],
            "secondaryOutcomes": [],
            "otherOutcomes": [],
        }
        key_for = {
            "primary": "primaryOutcomes",
            "secondary": "secondaryOutcomes",
            "exploratory": "otherOutcomes",
        }
        for level, measure, description, time_frame in outcomes:
            entry = {"measure": measure}
            if description:
                entry["description"] = description
            if time_frame:
                entry["timeFrame"] = time_frame
            grouped[key_for[level]].append(entry)

        studies.append(
            {
                "_synthetic": True,
                "protocolSection": {
                    "identificationModule": {
                        "nctId": study_id,
                        "briefTitle": f"A Study of an Investigational Agent in {conditions[0]}",
                        "officialTitle": (
                            f"A Randomised, Double-Blind, Placebo-Controlled Study of an "
                            f"Investigational Agent in Participants With {conditions[0]}"
                        ),
                    },
                    "statusModule": {
                        "overallStatus": "COMPLETED",
                        "startDateStruct": {"date": "2023-01-15"},
                        "primaryCompletionDateStruct": {"date": "2025-06-30"},
                        "completionDateStruct": {"date": "2025-12-31"},
                        "lastUpdatePostDateStruct": {"date": "2026-02-10"},
                    },
                    "sponsorCollaboratorsModule": {
                        "leadSponsor": {"name": sponsor, "class": "INDUSTRY"}
                    },
                    "conditionsModule": {"conditions": conditions},
                    "designModule": {
                        "studyType": "INTERVENTIONAL",
                        "phases": phases,
                        "enrollmentInfo": {"count": 400 + index * 37},
                    },
                    "outcomesModule": {k: v for k, v in grouped.items() if v},
                },
                "derivedSection": {
                    "conditionBrowseModule": {"meshes": [{"term": c} for c in conditions]}
                },
                "hasResults": index % 3 == 0,
            }
        )
    return {"studies": studies}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = build()
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    outcomes = sum(
        len(o)
        for s in payload["studies"]
        for o in s["protocolSection"]["outcomesModule"].values()
    )
    print(f"wrote {OUT} : {len(payload['studies'])} studies, {outcomes} outcomes")
