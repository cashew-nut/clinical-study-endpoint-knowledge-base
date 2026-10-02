# The vocabularies

One YAML file per dimension, plus the MeSH mapping and the matching contract.
Revised in vocabulary review round three against a 38,857-row pull whose two
largest therapeutic areas are cardiovascular and respiratory; round two was
against an unbiased sample of 13,542 outcome rows. This file records the schema,
the judgment calls, and the things a reviewer should push back on.

```
forms.yaml               18 terms   what kind of number the endpoint is
measurements.yaml       242 terms   what quantity or event it is about
derivations.yaml         14 terms   how the reported value is derived from the observations (trough, peak, slope...)
references.yaml          17 terms   what it is measured against
directions.yaml           7 terms   which way is better (derived, not matched)
events.yaml              38 terms   what occurrence ends the clock, for a time-to-event endpoint
scales.yaml              67 terms   the unit
therapeutic_areas.yaml   24 terms   the TA, derived from MeSH
drug_classes.yaml       116 terms   what the study was TESTING, derived from its interventions
timepoint_patterns.yaml  11 terms   time_frame categories + extraction regexes
ta_mesh_mapping.yaml                MeSH condition/intervention -> TA
drug_class_mesh_mapping.yaml        registered intervention -> drug class
matching.yaml                       HOW all of the above are matched
named_endpoints.yaml     12 defs    what a literature endpoint NAME (PFS, OS, MACE...) means
```

`matching.yaml` is not a term list. It is the contract the conforming pipeline
must implement: whole-token synonyms, case-sensitive acronyms, longest-match
where a file declares no precedence, and which field to read when the first one
is silent. It exists because leaving those rules implicit cost 9.8% of the
corpus to a single mis-matching acronym; see "Coverage, honestly" below.

Validate and load them with:

```bash
uv run endpoints vocab validate               # checks, then writes vocab.* tables
uv run endpoints vocab validate --check-only  # checks only
uv run endpoints vocab validate --strict      # warnings become errors
```

## Common schema

Every dimension file is a mapping with `version`, `dimension`, and `terms`:

```yaml
version: 1
dimension: form
terms:
  - id: time_to_event          # snake_case, unique within the file
    label: "Time-to-event"     # human-readable
    definition: >-             # what it means, in one or two sentences
      Time elapsed from a defined time origin until ...
    synonyms: ["survival", "time to progression"]
    patterns: ['\btime (to|from)\b']   # regex, matched case-insensitively
    notes: >-                  # why the boundary is drawn where it is
      Distinct from event_free_rate_at_timepoint because ...
```

`version` makes the files versioned, and the per-dimension keys below
(`match_precedence`, `direction_rule`, `precedence`, `priority`) have nowhere
else to live.

Files that need a match order carry one explicitly rather than relying on file
order: `match_precedence` in `forms.yaml` and `references.yaml`, `priority` in
`timepoint_patterns.yaml`, `precedence` in `therapeutic_areas.yaml`. The
validator checks each is complete and unambiguous, because in every one of them
the order changes the answer.

Each file also declares what happens when nothing matches
(`default_when_unmatched`). Only `measurements.yaml` has no fallback: it
declares `on_unmatched: review_queue`, so an unrecognised measurement stays
unconformed rather than hiding behind a conformed-looking row. Below threshold
goes to the review queue, and nothing is auto-added.

## The decisions worth reviewing

Each of these went a particular way for a reason. If any is wrong, it is cheap
to change now and expensive later.

### 1. Non-efficacy endpoints are in scope, tagged rather than excluded

About a third of registry outcomes are safety, PK, or immunogenicity, not
efficacy. Every measurement carries a `domain`, so `WHERE domain = 'efficacy'`
is one predicate, and adverse events, Cmax and ADA titres are conformed like
anything else rather than dumped in the review queue. They also conform
cleanly: PK parameters are the tidiest vocabulary in the corpus, and
"incidence of TEAEs", "number of TEAEs" and "time to first TEAE" are a
same-measurement-different-form triple.

### 2. Measurements are named at instrument level and also carry a `concept`

`PASI` and `sPGA` are separate measurements (different instruments) sharing no
id, but both carry a concept, `psoriasis_severity` and
`skin_disease_global_severity`. "Same measurement, different form" is then a
`GROUP BY id` and "same concept, different instrument" a `GROUP BY concept`,
with no extra vocabulary. If concept later needs its own definitions and
synonyms, promoting it to `concepts.yaml` is additive, since the join key
already exists.

### 3. Direction is derived, never stored on the endpoint's text

Registry text almost never states direction. So `forms.yaml` carries a
`direction_rule` and `measurements.yaml` carries the polarity of the underlying
quantity (`default_direction`) or of the event (`event_polarity`), and the
pipeline combines them. `vital_status` alone yields three different directions:

| endpoint | form | direction |
|---|---|---|
| Overall survival | `time_to_event` | `longer_is_better` |
| 30-day mortality | `incidence_proportion` | `decrease_is_better` |
| 2-year OS rate | `event_free_rate_at_timepoint` | `increase_is_better` |

Two refinements this forced, both of which caught real errors in fixture tests:

* **`event_polarity` is not always a property of the measurement.** RECIST
  tumour assessment hosts both "time to progression" (harm, longer is better)
  and "time to response" (benefit, sooner is better). `directions.yaml` carries
  `event_polarity_cues` matched against the endpoint text, which take precedence
  over the measurement's default.
* **`direction_by_ta`** overrides the default where polarity depends on
  indication: body weight falls in obesity and rises in cachexia.

### 4. Forms split where the split changes the answer

Sixteen forms. The splits that matter:

* `responder_proportion` vs `incidence_proportion`: identical arithmetic,
  opposite direction. Folding them would invert direction across the largest
  group in the corpus. Where wording cannot separate them, `forms.yaml`'s
  `disambiguation` block decides on the measurement's `event_polarity`.
* `event_free_rate_at_timepoint` vs `time_to_event`: a landmark proportion is a
  different estimand from a survival distribution, and "same measurement,
  different form" between them is what this warehouse exists to surface.
* `change_from_baseline` vs `percent_change_from_baseline`: reported side by
  side as separate endpoints in the same obesity and lipid trials.
* `not_stated` vs `descriptive`: a bare instrument name ("HbA1c", "PASI") is
  conformable with an unknown form, while "Safety and Tolerability" names no
  quantity at all and is marked `analysable: false`.

`not_stated` is the single largest form outcome: **31% of sampled measure
strings name a measurement and no form**. That is a property of registry text
rather than a gap in the vocabulary. The conforming pipeline upgrades those from
`time_frame`, where a `baseline_to_timepoint` pattern implies
change-from-baseline, and records the upgrade in `match_method` rather than
passing it off as an exact match.

### 4a. Trough, peak and AUC are a derivation, not a measurement or a form

"Change from baseline in FEV1 AUC0-3" used to land in one of two wrong
places. With a spaced "AUC" it became `auc_over_time`, which sat above
`change_from_baseline` in `match_precedence`. Written "AUC0-3" it missed
that form's `\bAUC\b` and hit `pk_auc` on span length instead, taking a
neutral direction and ng*h/mL for a lung-function endpoint in litres.

The endpoint is a change from baseline (form) in FEV1 (measurement), where
the FEV1 value is the area under the post-dose curve. That last part is its
own dimension, `derivations.yaml`, NULL when none is named. Trough, peak and
AUC FEV1 all keep `measurement_id = fev1`, so the SAME_MEASUREMENT join still
spans them, and `derivation_id` tells them apart. C-peptide follows the same
rule (`c_peptide` + `auc`).

`auc_over_time` stays for an AUC reported as the value itself ("C-peptide
AUC at Week 52", "AUC0-24 of drug X") and now sits below every
baseline-comparison form in `match_precedence`. `pk_auc` carries a
`not_if_matches` veto for response analytes (FEV1, C-peptide, glucose...) next
to the AUC. The USDM templates render `[{derivation} ]{measurement}` so the
text says "area under the curve of FEV1" while the measurement surrogate stays
plain FEV1. `endpoints stats` groups by derivation as well as form and unit,
and `--derivation` narrows to one.

### 4b. Derivation covers every way observations become the reported value

The dimension started as `summaries.yaml` with three FEV1-shaped terms. It is
now `derivations.yaml`: how the value an endpoint reports is derived from
potentially many observations of one measurement, in any therapeutic area.
Fourteen terms, each with a `kind` (what it does with the observations) and a
`span` (which observations it draws on):

| kind | terms | span |
|---|---|---|
| position | `trough` | within a visit |
| extreme | `peak`, `nadir`, `worst` | any window |
| extreme | `best_attempt` (best of three manoeuvres) | within a visit |
| aggregate | `replicate_mean` (triplicate ECG, seated BP) | within a visit |
| aggregate | `period_mean` (24-hour ABPM, weekly diary average) | within a period |
| aggregate | `visit_mean` (averaged over Weeks 10 and 12) | across visits |
| aggregate | `auc`, `cumulative` | any window |
| threshold | `proportion_of_time` (time in range, T90, AF burden) | within a period |
| persistence | `confirmed` (12-week confirmed progression, sustained eGFR decline) | across visits |
| dispersion | `variability` | any window |
| trend | `slope` (eGFR slope, rate of FVC decline) | across visits |

No definition names a measurement, and the validator checks `kind`, `span`
and that `match_precedence` lists every term. The precedence puts the most
specific derivation first, so "weekly average of daily worst itch" is `worst`
and "slope of trough FEV1" is `slope`.

Boundaries worth pushing back on:

* **The three means are a participant's own mean.** "Mean change from
  baseline" is the analysis mean across participants and gets no derivation,
  and so does a repeated-measures model's overall effect across visits unless
  the text says the visits were averaged. Only a mean tied to replicates, a
  recording period or a set of visits counts.
* **`nadir` is split between derivation and reference.** "PSA nadir" reports
  the low point and is derivation `nadir`; "25% rise over nadir" compares
  against it and is reference `nadir`. Each vetoes the other's phrasing, so a
  bare "nadir" no longer sets the reference.
* **`confirmed` needs a repeat-assessment phrase.** Bare "confirmed" more often
  says how a case was ascertained ("laboratory-confirmed influenza"), so only
  "confirmed/sustained" followed by a response, progression, decline and
  similar, or "on two consecutive visits", counts.
* **Left out on purpose:** best overall response (response criteria already
  define it, so tagging would split ORR by wording), imputation rules (LOCF,
  worst-case), time of day and window length (the timepoint's), and counts or
  rates of events per period (forms).

### 5. Reference spans time origins and value references, tagged by `kind`

"Randomisation" and "Baseline sum of diameters" both answer "relative to
what?", so they share a dimension. `kind`, one of `time_origin`,
`value_reference` or `external_standard`, keeps them separable in SQL without
splitting it.

`nadir` is a separate term from `patient_baseline`: RECIST progressive disease
is defined against the nadir rather than against baseline, and conflating them
is an analytic error.

### 6. Timepoint is classified, not looked up

`time_frame` is unbounded free text, so `timepoint_patterns.yaml` is a priority-
ordered classifier with extraction regexes, and the raw string is always kept.
Measured against the sample it classifies **96.6%** of outcome rows, and the
per-category distribution is recorded in the file as `coverage_on_sample` as a
regression baseline.

Two rules there are conventions rather than certainties, and are the most likely
to want changing:

* ~~"Baseline **through** Week 52" reads as a collection window; "Baseline
  **to** Week 52" reads as an assessment at a horizon.~~ **Overturned in round
  two.** The joined export shows every connective after a baseline token is
  change-majority; all four now match `baseline_to_timepoint`, and the
  `disambiguation` block hands the ambiguous "through" case to the form
  dimension. See "A round-one decision that the data overturned" below. This is
  the one round-one judgment call real data reversed, and it is left struck
  through rather than deleted so the correction stays visible.
* A bare "6 weeks" is a duration rather than "at Week 6". The discriminator is
  positional: a unit label before the number is a timepoint, and a number before
  the unit is a duration.

### 7. TA mapping is layered so it works on both ingestion backends

`term_overrides` (exact descriptor) → `tree_prefixes` (MeSH tree number) →
`term_patterns` (descriptor regex). AACT can join tree numbers; the CT.gov API
exposes only coarse branch letters, and the regex layer works on both. A study
keeps all matching areas, and `therapeutic_areas.yaml`'s `precedence` picks a
primary: oncology beats the organ system it appears in, so a lung cancer trial
is an oncology trial.

Vaccines are the exception. A vaccine trial codes its condition as the infection
it prevents rather than as "Vaccines", so that area is derived from intervention
MeSH codes in a separate `intervention_rules` block.

### 8. Drug class means mechanism, and `kind` is mandatory

`drug_classes.yaml` is the second study-attribute vocabulary, and it repeats the
therapeutic-area split: a term list plus a layered mapping in
`drug_class_mesh_mapping.yaml`. The decision worth pushing back on is what a
class means.

MeSH classifies a substance twice: structurally (metformin is a biguanide)
and functionally (metformin is a hypoglycemic agent). For grouping endpoints
the functional axis carries the signal: two drugs that lower HbA1c through the
same receptor should behave alike on an HbA1c endpoint, and two that share a
ring system should not. So every term declares a `kind`, one of `mechanism`,
`pharmacologic`, `modality` or `control`, the validator enforces it against a
closed set, and **structural class is absent rather than approximated**. A
`GROUP BY` that mixes kinds compares "PD-1 inhibitor" against "monoclonal
antibody" as though they were alternatives.

Two consequences, both enforced by `vocab validate`: a `parent` may not change
`kind`, since a rollup that changed axis halfway up would be meaningless, and a
child must have a lower `precedence` than its parent, so `pd1_inhibitor` wins
the primary over `checkpoint_inhibitor` and the specific claim is the one
grouped by.

`control` is a first-class kind because half the arms in a placebo-controlled
corpus are placebo arms. An axis that cannot name them cannot exclude them, and
what a class does relative to placebo is the first question asked of this axis.

The mapping's third layer is **WHO INN stems**. `-gliflozin` is not a guess
about a drug, it is the published stem that made the drug's name what it is, so
it classes agents NLM has not coded and agents approved after this file was
written. `agent_names` is a snapshot and drug dictionaries perish; see that
file's `caveats`.

### 9. Matching rules are vocabulary, not implementation (added in round two)

`matching.yaml` states how a synonym is compared to a registry string. That
looks like an implementation detail and is not. The obvious reading, substring
containment, inflated measurement coverage by 23 points while assigning 9.8% of
every outcome row in the corpus to a nosebleed-severity instrument, because
`ess` occurs inside "assessment". A rule that decides which measurement a tenth
of the corpus lands in belongs with the terms, versioned alongside them and
checked by the validator.

The three rules that carry the weight: whole-token synonym matching, case
sensitivity for all-caps acronyms of five characters or fewer, and
longest-match-wins in files that declare no precedence, which is what makes
`iwqol_lite` beat `body_weight` on "Impact of Weight on Quality of Life-Lite"
without a priority list across 165 terms.

## Coverage, honestly

These are round-two numbers, measured on the unbiased sample, and they are left
as they were measured: the round-three figures are reported separately under
"What round three changed" below, because they answer a different question
against a different corpus and averaging the two would hide the therapeutic-area
gap that was the finding. **They are lower than round one's and they mean
more.** Round one measured against the 500 most frequent distinct strings per
field, almost pure head, and scored 96.6% and 92%. Round two measures against
every value occurring twice or more plus a seeded random draw from the
singleton tail, then weights each sampled singleton back up to the corpus rows
it stands for.

The corpus behind these figures: **13,542 outcome rows, 11,230 distinct
`measure` strings.** 77% of `measure` rows are strings that occur exactly once.

| dimension | from `measure`/`time_frame` | + `description` | total |
|---|---|---|---|
| Form | 62.0% | 14.0% | **76.0%** |
| Measurement | 56.0% | 8.5% | **64.5%** |
| Timepoint (from `time_frame`) | 90.8% | — | **90.8%** |
| Reference (from `time_frame`) | 44.2% | — | **44.2%** |

Cascade totals come from `endpoint_rows.csv` (1,000 joined outcome rows, drawn
separately from the sample above). The population-weighted sample agrees:
93.1% for timepoints, 63.9% form and 56.7% measurement from `measure` alone.
Two independent estimates within sampling error of each other is the reason for
measuring both.

### The number that mattered most was not a coverage number

Round one's measurement coverage was **77.8%**. Round two's is 54.9% from
`measure`. Almost none of that 23-point drop is lost recall. It is a matching
defect a coverage number cannot see.

Synonyms were being matched as substrings. `ess` matched inside "assessment"
and "progression", assigning **9.8% of every outcome row in the corpus** to
`epistaxis_severity_score`, a nosebleed instrument used in one rare-disease
indication. `alt` and `ast` matched inside "Health" and "Past" → `liver_enzymes`.
`fa` inside "Fatigue" and "Factor" → `fluorescein_angiography_findings`. `ree`
inside "preeclampsia" and "-free" → `resting_metabolic_rate`. Every one of those
inflated coverage while corrupting the same-measurement comparisons the
warehouse exists to support.

The fix is `matching.yaml`, a new file: whole-token matching, case-sensitive
short acronyms, longest-match-wins where a file declares no precedence, and the
field cascade. Matching rules are now part of the vocabulary rather than left to
whoever writes the matcher. Two further defects fell out of writing it down:
"AEs Leading to Discontinuation" resolving to the generic `adverse_event`, and
"Impact of Weight on Quality of Life-Lite" resolving to `body_weight` on the
word "weight". Both are fixed by longest-match, with no list to maintain.

### What round two changed

* **`matching.yaml`**, new. The contract above.
* **`forms.yaml`**: `event_free_days` and `correlation` added; a guarded generic
  `<good outcome> rate` pattern, since the closed prefix list was catching none
  of "R0 Resection Rate", "Sputum Culture Conversion Rate" or "MRD Negativity
  Rate"; `not_if_matches` so physiological rates stay out of it; and
  bare-acronym patterns (OS, PFS, ORR, pCR…) matched case-sensitively. 63.9%
  from `measure`, up from 58.6%.
* **`timepoint_patterns.yaml`**: 88.4% → 93.1%. Parenthetical dropping,
  hyphenated units, ordinal timepoints, minutes, an open-ended anchor, and
  intervals between two non-baseline timepoints. Plus one **reversal**, below.
* **`measurements.yaml`**: 12 terms added, each from a repeated miss: the
  standard neurocognitive battery (HVLT-R, BVMT-R, TMT, COWAT, digit symbol),
  `surgical_margin_status`, `pathological_response`,
  `biochemical_marker_normalisation`, `disease_progression_event` and others.
* **`ta_mesh_mapping.yaml`**: 12 disagreements found against 80 real MeSH
  descriptors, 9 fixed. See below.

### A round-one decision that the data overturned

Round one sent `Baseline through Week 52` to `cumulative_window` and
`Baseline to Week 52` to `baseline_to_timepoint`, on the linguistic argument
that "through" describes a window and "to" describes a point. **That argument is
wrong.** With the joined export it can be checked directly, by asking what form
the paired `measure` resolves to:

| connective after a baseline token | n | change-family form | cumulative-family form |
|---|---|---|---|
| "to" | 104 | 60 | 18 |
| "and" / "," | 109 | 63 | 9 |
| "up to" | 24 | 19 | 5 |
| "through" | 21 | 9 | 8 |

Every connective is change-majority, so all four now match
`baseline_to_timepoint`. "Up to" is decisive at 79%. "Through" is a coin flip
and ambiguous in the source: the same corpus has "Baseline through Week 52"
against both "Change from Baseline through Week 216" and "Annualized asthma
exacerbation rate over 52 weeks". The connective does not carry the
distinction and the form does, so `timepoint_patterns.yaml` has a
`disambiguation` block that hands the call to the form dimension.

### The cascade, and one rule that is absent

Reading `description` when `measure` names no form recovers 36.1% of the
otherwise-unmatched rows, 140 of 1,000. That is why `matching.yaml` specifies a
field cascade, and why a cascade hit is recorded as `syntactic_rule` rather than
`exact`.

It is also why there is **no rule mapping a bare "Adverse Events" title to
`incidence_proportion`**, despite an estimated 380 corpus rows. Of the eight
bare safety-titled outcomes in the joined export, five have a description
saying "incidence and severity of adverse events" outright. The registry
answers the question itself in most cases, and where it does not, `not_stated`
is correct where a lexical guess is not.

References are the mirror image: 17.4% of rows from `measure` and **44.2% from
`time_frame`**. The time origin of an endpoint is written into the timing field
rather than the title, so the reference cascade reads `time_frame` first.

## What round three changed: the vocabulary was oncology-shaped

Rounds one and two were measured against samples drawn without regard to
therapeutic area, and the vocabulary they produced was written the same way,
which meant oncology, given where the reference tables and the reviewers'
attention came from. Round three is the first round measured on a corpus that
is not:
**38,857 outcome rows over 4,345 studies, whose two largest areas are
cardiovascular (9,160 rows) and respiratory (7,322), against oncology's 6,588.**

The bias was invisible in the headline coverage number and obvious the moment
the number was cut by therapeutic area:

| therapeutic area | rows | measurement conformed, before | after |
|---|---|---|---|
| cardiovascular | 9,160 | 53.0% | **73.5%** |
| respiratory | 7,322 | 64.7% | **77.4%** |
| oncology | 6,588 | 79.2% | 80.3% |
| whole corpus | 38,857 | 61.7% | **70.5%** |

Twenty-six points between the best-covered area and the worst is not a property
of registry text. Cardiovascular endpoints are if anything more stereotyped
than oncology ones, since the same adjudicated event list recurs trial after
trial. The gap was the library.

The same bias sat in the event axis, where being wrong is louder. Event
resolution on event-family rows ran **72.0% in oncology against 48.8%
cardiovascular and 47.7% respiratory**; it is now **73.2% / 72.9% / 59.2%**. An
event-family row whose event does not resolve renders at `partial` tier, which
is correct. One that resolves to the wrong event renders a confident,
standards-conformant, clinically wrong sentence, which is what
`docs/EVENT_SEMANTICS_SPEC.md` was written to prevent and which was still
happening outside oncology:

* `Major adverse cardiovascular events within 30 days (MACE30)` resolved its
  event to `death_any_cause`, and `Major adverse cardiovascular event (MACE)` to
  `stroke`, both off the components spelled out in the description, because
  the event cascade reads `description` when `measure` yields nothing and MACE
  had no event of its own to yield.
* An endpoint counting **cardiovascular** death resolved to `death_any_cause`,
  the strictly wider event, for want of a narrower term.

### What was added

* **`measurements.yaml`: 69 terms** (165 → 234), 40 cardiovascular and 29
  respiratory, every one from a repeated miss in the review queue rather than
  from a textbook. The cardiovascular block leads with the adjudicated events:
  infarction, stroke, bleeding, revascularisation, stent thrombosis,
  VTE, atrial fibrillation, arrhythmia, cardiac arrest, unstable angina, limb
  events, LVAD and transplant, worsening heart failure. Then the quantities
  (blood pressure unspecified, MAP, LV volume and mass, strain, diastolic
  function, troponin, infarct size, cardiac output, the four missing lipid
  fractions, KCCQ/MLHFQ/SAQ, peak VO2, platelet reactivity, arterial stiffness,
  endothelial function, decongestion) and the serum chemistry every diuretic and
  RAAS trial reports. The respiratory block covers what spirometry misses (PEF,
  static lung volumes, oscillometry, LCI), the type-2 biomarkers (FeNO, blood
  and sputum eosinophils), the PRO instruments that dominate COPD and asthma
  (CAT, E-RS, ACT, the symptom diaries, mMRC, TDI, cough, K-BILD), sweat
  chloride, corticosteroid-sparing, respiratory support and blood gases, plus
  the right-heart-catheter variables of pulmonary hypertension, which
  `ta_mesh_mapping.yaml` routes to `respiratory`.
* **`events.yaml`: 15 terms** (23 → 38): the cardiovascular event classes,
  `cardiovascular_death`, the MACE composite, respiratory support escalation
  and failure, and the two benefit events (`hospital_discharge`,
  `ventilator_weaning`) the direction diff below forced. Per this file's
  cross-file synonym discipline the wording lives on the measurement and the
  event is reached through `implies_event`; the exceptions are argued in
  place.
* **`scales.yaml`: 8 units** (59 → 67): L/min, m/s, kPa, Wood units,
  dyn·s·cm⁻⁵, µmol/L, ppb, mg/day.
* **`named_endpoints.yaml`: 2 definitions**: `ttcw` (time to clinical
  worsening, the pulmonary-hypertension analogue of PFS) and `daoh`.
* **Three existing measurements were wired to events that already existed** and
  had simply never been pointed at: `hospitalisation` → `hospitalisation_event`
  (279 rows), `treatment_discontinuation`, `intracranial_haemorrhage` →
  `bleeding_event`. One line each, and between them a third of the
  non-cardiovascular event-coverage gain above.
* **`directions.yaml`** gained two cardio-respiratory harm cues, as the
  documented fallback for rows whose event does not resolve.

Of 234 measurement terms, **exactly one (`graves_orbitopathy_qol`) fails to fire
anywhere in this pull**, which retires round two's "`fev1`, `easi`,
`madrs`, `edss`, `womac` and 20 other terms have never fired on any sample"
caveat. FEV1 alone carries 727 rows here.

### MACE is a measurement, not a named endpoint

Giving MACE a `named_endpoints.yaml` definition, the way PFS has one, is the
wrong shape. A named-endpoint definition pins a form, and MACE is as often an
incidence proportion as a time-to-first-event in this corpus, so pinning
`time_to_event` would be the lexical guess the cascade section below refuses to
make for bare "Adverse Events". MACE stays a measurement whose `implies_event`
reaches a MACE event, which keeps the `SAME_MEASUREMENT_DIFFERENT_FORM` join
between "incidence of MACE" and "time to first MACE". All 97 event-family MACE rows
in this corpus now resolve to the MACE event; before round three, none did.

### Five over-matching synonyms, caught by diffing against the previous run

Round two's lesson was that a coverage number cannot see a matching defect. So
round three was measured the other way as well: every row conformed before
these terms was re-conformed after, and the roughly 1,460 that changed
measurement were read, along with every row whose direction changed while its
measurement did not. Most were fixes: "Transient ischemic attack" leaving
`disease_exacerbation`, where it had matched the synonym "attack", "Lung
Clearance Index" leaving `pk_clearance`, and "Major bleeding" leaving
`haemoglobin`. Five were defects in the new terms, all the same failure mode as
round two's `ess`:

| synonym | what it also matched | fix |
|---|---|---|
| `Raw` (airway resistance) | "raw score", in 8 PROMIS/rating-scale descriptions | dropped; `sRaw`/`siRaw` kept |
| `GCS` (global circumferential strain) | Glasgow Coma Scale, 4 of 5 occurrences | dropped, spelled out |
| `NCS` (nasal congestion score) | "Abnormal Not Clinically Significant (NCS)", 8 of 25 | dropped, spelled out |
| `RHI` (reactive hyperaemia index) | Robarts Histopathology Index, 4 of 14 | dropped, spelled out |
| `exercise capacity` (peak VO2) | 10 rows that said only "Functional Exercise Capacity" | dropped: it names the shared concept, not this instrument |

Two more were caught before they shipped, from the corpus rather than from a
diff: `creatinine` is longer than both `UACR` and `eGFR` and so took
"urine albumin-to-creatinine ratio" and "eGFR (CKD-EPI creatinine equation)"
rows off them, fixed with two `not_if_matches` vetoes; and bare `sodium` /
`potassium` are the commonest drug-salt suffix in pharmacology (`amogammadex
sodium`, `sodium zirconium cyclosilicate`), so both are matched only with a
`serum`/`plasma`/`urinary` qualifier.

The direction half of that diff found the one defect a measurement diff cannot
show. Wiring `hospitalisation` to `hospitalisation_event`, polarity `harm`, is
right for an admission endpoint and wrong for "Time to discharge", which flipped
from `shorter_is_better` to `longer_is_better` because direction is event-first
and the discharge had no event of its own. The fix is two benefit events,
`hospital_discharge` and `ventilator_weaning`, rather than a retreat from the
wiring. `directions.yaml` has listed "time to ... discharge" and "...
extubation" among its benefit cues since it was written, so this is the same
judgment moved to the layer that decides ahead of the cues.

Three acronyms were left out on the same evidence and are recorded in their
terms' notes: `PE` (physical examination and plasma exchange as well as
pulmonary embolism), `BDI` (the Beck Depression Inventory, in 54 of 56
occurrences, not the Baseline Dyspnea Index) and `PCI` (as often the index
procedure a trial enrols after as the outcome event). `MI`, `MALE`, `CAT`, `ACT`
and `MAP` were kept but carry either a case-sensitive pattern, a
`not_if_matches` veto, or both.

## What round four changed: `scales.yaml` became a conversion table

Round four served [`docs/ENDPOINT_RESULTS_SPEC.md`](../docs/ENDPOINT_RESULTS_SPEC.md)'s
dispersion normaliser (D6). On the protocol side a scale is mostly a label; on
the results side it decides whether two trials' numbers can be pooled at all,
because `unit_of_measure` is sponsor-written free text and FEV1 arrives in both
litres and millilitres.

* **`factor_to_si` went from 21 of 59 terms to 58 of 67.** Every family whose
  conversion is exact unit algebra now has one: flow, velocity, pressure,
  vascular resistance, mass concentration, molar concentration, BMI and %BSA.
  The singleton families self-anchor at factor 1, so a converted SD column is
  populated rather than null for them.
* **Nine terms stay unconvertible on purpose**, each with the reason on the
  term: `percent_change` and `ratio` are different quantities rather than two
  spellings of one; `percent_hba1c` ↔ `mmol_per_mol` is affine, not a factor;
  the mass-concentration and molar-concentration families are not linked,
  because `mg/dL ↔ mmol/L` needs the analyte's molar mass, a property of the
  measurement rather than of the unit; the immunogenicity titres and
  `count_per_period` are left for a reviewer; `dimensionless` and `not_stated`
  are catch-alls.
* **Anchors are now validated.** A term naming another as its `si_equivalent`
  requires that term to declare `si_equivalent: <itself>` and
  `factor_to_si: 1`. Without it, a conversion could land in a unit that is
  itself expressed in something else, leaving the comparability column one
  factor out.
* **Synonyms gained the results register**: the plural and American spellings
  the `unit_of_measure` field uses, and the phrases that are units only in that
  field ("units on a scale", "number of participants"). A unit field can be
  matched whole, so `results/units.py` resolves a bare `L` that
  `matching.yaml`'s `min_synonym_length` rule refuses to match inside a
  sentence.

No new scale terms were added, so the count stays at 67.

## Known gaps

* **`drug_classes.yaml` and `drug_class_mesh_mapping.yaml` have never been run
  against a live pull, and the gap is different in kind from the ones below.**
  The other vocabularies were revised against a real 38,857-row export. The
  drug-class pair was written from the documented CT.gov API v2 shape and from
  published MeSH and WHO INN naming conventions, and its scope was chosen to
  match the therapeutic areas already covered rather than measured against what
  a corpus contains. Two of its layers, MeSH ancestors and browse branches,
  have no AACT equivalent and have never been observed on the CT.gov side
  either. `docs/DRUG_CLASS_SPEC.md`'s "Phasing, with a gate" lists the four
  counts a first live pull owes it, and what happens to the axis under each bad
  answer. `drug_class_mesh_mapping.yaml`'s own `caveats` block says the same in
  its first line. Read the coverage number from
  `endpoints drug-class coverage` before trusting the distribution.
* ~~**Live AACT/CT.gov access is still unavailable from this build sandbox.**~~
  **Partly closed.** Round three was measured against a real pull's export:
  38,857 `design_outcomes` rows over 4,345 studies, plus the TA resolver's own
  output for those studies, in which all four `ta_mesh_mapping.yaml` rule layers
  (`term_override`, `tree_prefixes`, `term_pattern`, `intervention_rule`) are
  present. So the vocabulary, the matcher, the conforming pipeline and the TA
  resolver have now all seen real registry text. `pull` itself and
  `endpoints ta diff-tree` still have not been re-run from this sandbox.
* ~~**The event-semantics work has the same limitation, one measurement
  short.**~~ **Measured in round three.** `docs/EVENT_SEMANTICS_SPEC.md` gated
  its phase D on event coverage from a real pull; here it is. Over the 10,154
  event-family rows in this corpus, `event_match_method` splits
  `named_endpoint` 2,739 / `implied` (a measurement's `implies_event`) 2,431 /
  `exact` 966 / `syntactic_rule` 323, with 3,695 unresolved, so 63.6% resolved
  overall, and the per-area split under "What round three changed" above.
  `endpoints usdm coverage` over the same corpus moves from templated 33.0% /
  partial 21.7% / verbatim 45.3% before round three to **38.0% / 24.8% /
  37.2%** after, with `reference defaulted` steady at 4.3% → 4.5% of templated.
  Two caveats on those figures. The pull's export carried no
  `raw.studies.allocation`, so the randomisation gate on a named-endpoint
  definition's `reference` never opened. Every PFS, OS and TTCW row here
  resolved its reference through the ordinary cascade, and a pull that carries
  allocation should show more templated rows and a different defaulting rate.
  Phase D's own question, whether `incidence_proportion`, `event_count` and
  `event_rate` should switch from `{measurement}` to `{event}`, is now
  answerable and still unanswered: their corpus is no longer AE-dominated in
  cardiovascular, where `implies_event` reaches a real event on most rows.
* **The TA tree-vs-pattern diff was run against a hand-built truth set, not a
  live pull.** 80 real MeSH descriptors whose tree placement is known; 12
  disagreements, 9 of them defects now fixed (the three interstitial pneumonias
  routing to `infectious_disease`; `Dementia, Vascular` to `cardiovascular`;
  `thromb\w+` claiming every `Thrombocytopenia`; `\bthyroid\b` failing to match
  "Hypothyroidism" at all; "Dry Eye Syndromes" and "Cataract" matching no
  ophthalmology pattern; `Diabetic Nephropathies` resolving to
  `metabolic_endocrine`). Two are deliberate divergences from MeSH, recorded in
  that file's `caveats`: pulmonary hypertension → `respiratory`, sepsis →
  `anaesthesia_critical_care`. Re-run `endpoints ta diff-tree` against a real
  pull when one is possible; a hand-built truth set finds what its author thought
  to test.
* ~~**Rheumatology and respiratory are still thin.**~~ **Closed for respiratory
  and cardiovascular by round three; still open for rheumatology.** The
  38,857-row pull exercised every one of the terms round two could not:
  `fev1` carries 727 rows, `acr_response_composite` 44, `madrs` 33, `das28` 22,
  `easi` 21, `womac` 11, `edss` 10. Exactly one measurement term
  (`graves_orbitopathy_qol`) fails to fire anywhere in it. Rheumatology is a
  541-row area here and conforms at 61.7%, so it is under-sampled rather than
  demonstrably thin, which narrows this bullet's original "absence of evidence
  either way" to the areas the pull did not reach.
* **`adverse_event` and `serious_adverse_event` have no event term**, and
  between them account for 1,893 event-family rows carrying
  `event: not_stated`. That is the largest remaining event-coverage gap, and
  why respiratory event resolution (59.2%) still trails cardiovascular (72.9%).
  Round three left it open on purpose: an adverse-event event term changes the
  projection for every therapeutic area at once, which is a corpus-wide
  decision rather than a cardio-respiratory one. It is the next highest-value
  edit in this file.
* **Composite endpoints that name a union in free text still resolve to one
  member.** "Time from randomisation to first occurrence of arterial thrombosis,
  venous thromboembolism or cardiovascular death" gets `venous_thromboembolism`
  by longest match, which is defensible and lossy. MACE is handled because it
  has a name, while unions written out in full are not, and
  `docs/COMPOSITE_ENDPOINTS_SPEC.md` is where that belongs rather than here.
* **The residual 6.9% of unclassified timepoints is long tail**: multi-phase
  narrative schedules, per-cohort schedules, and labelled study periods with no
  absolute horizon. Chasing it would overfit.
* **Thresholds are parsed rather than vocabularised.** The comparator, value and
  unit regexes live with the conformance parser, and `forms.yaml` only flags
  which forms expect one via `expects_threshold`.

## Adding or changing a term

1. Edit the YAML.
2. `uv run endpoints vocab validate --check-only`.
3. `uv run pytest tests/test_vocab_loader.py`. The reference-table fixtures
   there are the acceptance criteria, so they should keep passing.

The validator rejects: duplicate ids, non-snake_case ids, a synonym claimed by
two terms in the same dimension, a regex that does not compile, a cross-file
reference to an id that does not exist, a `match_precedence` that has drifted
out of step with its terms, tied precedence or priority values, and a value
outside a closed set (`direction_rule`, `domain`, `event_polarity`, reference
`kind`).

---

## `usdm_templates.yaml` and `inline_label`

Added with the USDM 4.0 endpoints projection
(`docs/USDM_ENDPOINTS_API_SPEC.md`). Two changes touch this vocabulary.

### One syntax template per form

`usdm_templates.yaml` is not a term list. It holds one sentence frame per
`forms.yaml` id, because form is already defined here as "what kind of number
is this endpoint", independent of what is measured, which is a sentence frame
written out:

```yaml
- form: change_from_baseline
  template: "Change from {reference} in {measurement}[ {timepoint}][ ({scale})]"
  reference_fallback: patient_baseline
```

`{tag}` is a slot filled from a conformed endpoint's dimensions, and `[ ... ]`
is dropped whole when a tag inside it did not resolve. A tag outside brackets is
required, and a form whose required tags do not resolve renders verbatim rather
than as half a sentence. `vocab validate` rejects a form with neither a
template nor `verbatim: true`, a tag outside the closed set, a template with no
required tag, and a missing `{threshold}` on a form that declares
`expects_threshold`.

The file also carries `purpose_by_domain`, since USDM requires
`Endpoint.purpose` and no registry record states one, so it is derived from the
measurement's `domain`, and `objective_templates`, since USDM hangs endpoints
off objectives and registry records have none.

### `inline_label`: the sentence-fragment form of a term

A term's `label` is a display label, written for a review table. Several read
wrong inside a generated sentence:

```
'First dose / start of treatment'      -> 'the start of treatment'
'Percentage of participants (%)'       -> '%'
'No reference (absolute quantity)'     -> null
```

So `references.yaml`, `scales.yaml` and `measurements.yaml` carry an optional
`inline_label`, and a renderer uses it in preference to `label`. `null` means
the term has no sentence form at all: `references.yaml`'s `none` and
`not_stated` name the absence of a reference, and printing "Change from No
reference (absolute quantity) in FEV1" is worse than dropping the phrase.

`vocab validate` warns where a tag-reachable term has no `inline_label` and a
`label` that will not read as a fragment: one containing a slash, or a
parenthetical that is not a bare abbreviation. `Glycated haemoglobin (HbA1c)`
passes and `Ratio (dimensionless)` does not. That check found 50 terms on its
first run, all now curated.

---

## `events.yaml` and `named_endpoints.yaml`: the event axis

Added by `docs/EVENT_SEMANTICS_SPEC.md`, after a live-pull validation
(NCT01777919) showed the USDM projection rendering "Time from randomisation to
Tumour burden (RECIST)" for a progression-free survival endpoint, a confident,
standards-conformant and clinically wrong sentence. The model had dimensions
for the time origin (`reference`) and for what is assessed (`measurement`), and
none for the event a time-to-event endpoint counts down to. `event` is the
ninth dimension, and `named_endpoints.yaml` is what makes recognising a
literature name such as PFS, OS or DFS resolve the whole bundle of form, event,
reference and measurement at once, rather than donating the name to one
dimension as a synonym.

**`measurements.yaml` is unchanged in grain.** PFS and ORR still share
`tumour_burden_recist`. That shared id is the SAME_MEASUREMENT_DIFFERENT_FORM
join this project exists to build, and regraining it to event level would fix
one sentence by destroying that join. Instead, endpoint-name synonyms that used
to live on `tumour_burden_recist` / `vital_status` / `disease_recurrence` /
`treatment_failure` ("progression-free survival", "PFS", "overall survival",
"OS", "disease-free survival", "DFS" and so on) moved to
`named_endpoints.yaml`. Each measurement kept only its assessment-language
synonyms: RECIST, tumour response, and the mortality-rate phrasings that name
the ascertainment. A handful of event-shaped measurements (`vital_status`,
`disease_recurrence`, `disease_progression_event`, `treatment_failure`,
`disease_exacerbation`) gained `implies_event`, so a row whose measurement is
the event resolves it with no duplicated text match. `events.yaml` does not
repeat a synonym a measurement's `implies_event` already reaches, since the
validator rejects a synonym claimed by two of measurement, event and
named_endpoint.

**Event resolution is a `conform_row` step (0), run before the ordinary
per-dimension cascade**, not a projection-time inference:
`named_endpoints.yaml` match -> `events.yaml` match over `measure` ->
`description` -> the resolved measurement's `implies_event` -> `not_stated`.
It only runs for the six **event-family** forms (`forms.yaml`
`event_family: true`: `time_to_event`, `event_free_rate_at_timepoint`,
`event_free_days`, `incidence_proportion`, `event_count`, `event_rate`). That
set is declared explicitly rather than derived from `direction_rule`, because
direction does not carve this joint: `event_free_rate_at_timepoint` is
`higher_count_better` yet is entirely about an event, while
`shift_from_baseline` is `inherit_event_polarity` yet names none and stays
outside `event_family`. `vocab validate` warns rather than errors on that one
case, so it does not read as a defect.

A `named_endpoints.yaml` definition **fills** the measurement and reference
dimensions only when their own ordinary cascade is silent, and **fills** form
the same way rather than overriding it. forms.yaml's own cascade already
resolves a named endpoint's typical form, for instance "progression-free
survival" via `time_to_event`'s own synonym, and must stay free to route a
landmark phrasing like "2-Year Overall Survival" to
`event_free_rate_at_timepoint` instead. A definition's `reference` applies only
when `raw.studies.allocation` says the study is randomised, since asserting
"from randomisation" on a single-arm trial's PFS is the unannounced default
`docs/USDM_PROJECTION_INTEGRITY_SPEC.md` exists to prevent.

**Direction is event-first.** `directions.yaml`'s `event_polarity_cues`
free-text regexes are the fallback layer for rows whose event does not
resolve rather than the primary evidence. Resolution order is the resolved event's
own `polarity`, then the cues, then the matched measurement's
`event_polarity`, then `not_stated`.

**The USDM projection** (`docs/USDM_ENDPOINTS_API_SPEC.md`) gained `{event}`
as an eighth tag: a `BiomedicalConceptSurrogate` per distinct resolved event,
served at `/v4/vocab/event/{id}`, the same shape as `{measurement}`. Three
consequences follow. `time_to_event`, `event_free_rate_at_timepoint` and
`event_free_days` render `{event}` instead of `{measurement}`, with
`{reference}` optional rather than defaulted, since `reference_fallback` was
deleted: a form-keyed fallback asserted randomisation on DoR rows, whose origin
is `response_onset`, and on single-arm trials alike. An event-family row whose
event does not resolve degrades to the `{measurement}[ {timepoint}]` frame at
`partial` tier rather than rendering the assessment into the event's place. And
the measurement surrogate is minted for every conformed endpoint with a
resolved measurement, tag-referenced or not, so the PFS and ORR join does not
disappear from the document just because the sentence stopped mentioning
`{measurement}` directly. `incidence_proportion`, `event_count` and
`event_rate` keep `{measurement}` for now, because their corpus is
AE-dominated, where the measurement is the event and the current rendering
reads correctly. Switching them waits on measured event coverage; see
`docs/EVENT_SEMANTICS_SPEC.md` phase D.
