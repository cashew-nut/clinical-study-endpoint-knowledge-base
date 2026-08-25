# The vocabularies

One YAML file per dimension, plus the MeSH mapping and the matching contract.
Revised in vocabulary review round three against a 38,857-row pull whose two
largest therapeutic areas are cardiovascular and respiratory; round two was
against an unbiased sample of 13,542 outcome rows. This file records the schema,
the judgment calls, and the things a reviewer should push back on.

```
forms.yaml               18 terms   what kind of number the endpoint is
measurements.yaml       234 terms   what quantity or event it is about
references.yaml          17 terms   what it is measured against
directions.yaml           7 terms   which way is better (derived, not matched)
events.yaml              38 terms   what occurrence ends the clock, for a time-to-event endpoint
scales.yaml              67 terms   the unit
therapeutic_areas.yaml   24 terms   the TA, derived from MeSH
timepoint_patterns.yaml  11 terms   time_frame categories + extraction regexes
ta_mesh_mapping.yaml                MeSH condition/intervention -> TA
matching.yaml                       HOW all of the above are matched
named_endpoints.yaml     12 defs    what a literature endpoint NAME (PFS, OS, MACE...) means
```

`matching.yaml` is not a term list. It is the contract the conforming pipeline
must implement — whole-token synonyms, case-sensitive acronyms, longest-match
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

This is a superset of the shape sketched in the implementation plan (a bare
top-level list). The extra keys earn their place: `version` makes the files
genuinely versioned, and the per-dimension keys below (`match_precedence`,
`direction_rule`, `precedence`, `priority`) have nowhere else to live.

Files that need a match order carry one explicitly rather than relying on file
order — `match_precedence` in `forms.yaml` and `references.yaml`, `priority` in
`timepoint_patterns.yaml`, `precedence` in `therapeutic_areas.yaml`. The
validator checks each is complete and unambiguous, because in every one of them
the order changes the answer.

Each file also declares what happens when nothing matches
(`default_when_unmatched`). Only `measurements.yaml` has no fallback: it
declares `on_unmatched: review_queue`, so an unrecognised measurement stays
unconformed rather than hiding behind a conformed-looking row. That is the plan's
"below threshold → review queue, nothing auto-added" rule, made checkable.

## The decisions worth reviewing

Each of these went a particular way for a reason. If any is wrong, it is cheap
to change now and expensive later.

### 1. Non-efficacy endpoints are in scope, tagged rather than excluded

About a third of registry outcomes are safety, PK, or immunogenicity, not
efficacy. Every measurement carries a `domain`, so `WHERE domain = 'efficacy'`
is one predicate — but adverse events, Cmax, and ADA titres are conformed like
anything else rather than dumped in the review queue. They also conform
*cleanly*: PK parameters are the tidiest vocabulary in the whole corpus, and
"incidence of TEAEs" / "number of TEAEs" / "time to first TEAE" is a textbook
same-measurement-different-form triple.

### 2. Measurements are named at instrument level and also carry a `concept`

`PASI` and `sPGA` are separate measurements (different instruments) sharing no
id, but both carry a concept — `psoriasis_severity` and
`skin_disease_global_severity`. "Same measurement, different form" is then a
`GROUP BY id` and "same concept, different instrument" a `GROUP BY concept`,
with no extra vocabulary. If concept later needs its own definitions
and synonyms, promoting it to `concepts.yaml` is additive — the join key already
exists.

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
* **`direction_by_ta`** overrides the default where polarity genuinely depends
  on indication — body weight falls in obesity and rises in cachexia.

### 4. Forms split where the split changes the answer

Sixteen forms, not the three in the plan's illustrative example. The splits that
matter:

* `responder_proportion` vs `incidence_proportion` — identical arithmetic,
  opposite direction. Folding them would invert Direction across the largest
  group in the corpus. Where wording cannot separate them, `forms.yaml`'s
  `disambiguation` block says to decide on the measurement's `event_polarity`.
* `event_free_rate_at_timepoint` vs `time_to_event` — a landmark proportion is a
  different estimand from a survival distribution, and "same measurement,
  different form" between them is exactly what this warehouse exists to surface.
* `change_from_baseline` vs `percent_change_from_baseline` — reported side by
  side as separate endpoints in the same obesity and lipid trials.
* `not_stated` vs `descriptive` — a bare instrument name ("HbA1c", "PASI") is
  conformable with an unknown form; "Safety and Tolerability" names no quantity
  at all and is marked `analysable: false`.

`not_stated` is the single largest form outcome: **31% of sampled measure
strings name a measurement and no form**. That is a property of registry text,
not a gap in the vocabulary. The conforming pipeline should try to upgrade those
from `time_frame` (a `baseline_to_timepoint` pattern implies change-from-baseline)
and must record the upgrade in `match_method` rather than passing it off as an
exact match.

### 5. Reference spans time origins and value references, tagged by `kind`

The plan's reference table mixes "Randomisation" with "Baseline sum of
diameters", and that is right — both answer "relative to what?". `kind`
(`time_origin` / `value_reference` / `external_standard`) keeps them separable in
SQL without splitting the dimension.

`nadir` is a separate term from `patient_baseline` on purpose: RECIST
progressive disease is defined against the nadir, not against baseline, and
conflating them is a real analytic error.

### 6. Timepoint is classified, not looked up

`time_frame` is unbounded free text, so `timepoint_patterns.yaml` is a priority-
ordered classifier with extraction regexes, and the raw string is always kept.
Measured against the sample it classifies **96.6%** of outcome rows; the
per-category distribution is recorded in the file as `coverage_on_sample` so
step 3 can use it as a regression baseline.

Two rules there are conventions, not certainties, and are the most likely things
to want changing:

* ~~"Baseline **through** Week 52" reads as a collection window; "Baseline
  **to** Week 52" reads as an assessment at a horizon.~~ **Overturned in round
  two.** The joined export shows every connective after a baseline token is
  change-majority; all four now match `baseline_to_timepoint`, and the
  `disambiguation` block hands the genuinely ambiguous "through" case to the form
  dimension. See "A round-one decision that the data overturned" below — this is
  the one round-one judgment call that real data reversed, and it is left struck
  through rather than deleted so the correction is visible.
* A bare "6 weeks" is a duration, not "at Week 6". The discriminator is purely
  positional — unit label before the number is a timepoint, number before the
  unit is a duration.

### 7. TA mapping is layered so it works on both ingestion backends

`term_overrides` (exact descriptor) → `tree_prefixes` (MeSH tree number) →
`term_patterns` (descriptor regex). AACT can join tree numbers; the CT.gov API
exposes only coarse branch letters; the regex layer works on both. A study keeps
*all* matching areas, and `therapeutic_areas.yaml` `precedence` picks a primary
— oncology beats the organ system it appears in, so a lung cancer trial is an
oncology trial.

Vaccines are the exception: a vaccine trial codes its condition as the infection
it prevents, never as "Vaccines", so that area is derived from *intervention*
MeSH codes in a separate `intervention_rules` block.

### 8. Matching rules are vocabulary, not implementation (added in round two)

`matching.yaml` states how a synonym is compared to a registry string. That
looks like an implementation detail and is not: the obvious reading — substring
containment — inflated measurement coverage by 23 points while assigning 9.8% of
every outcome row in the corpus to a nosebleed-severity instrument, because
`ess` occurs inside "assessment". A rule that decides which measurement a tenth
of the corpus lands in belongs with the terms, versioned alongside them and
checked by the validator, not in whoever writes the matcher.

The three rules that carry the weight: whole-token synonym matching; case
sensitivity for all-caps acronyms of five characters or fewer; and
longest-match-wins in files that declare no precedence, which is what makes
`iwqol_lite` beat `body_weight` on "Impact of Weight on Quality of Life-Lite"
without anyone maintaining a priority list across 165 terms.

## Coverage, honestly

These are round-two numbers, measured on the unbiased sample, and they are left
as they were measured: the round-three figures are reported separately under
"What round three changed" below, because they answer a different question
against a different corpus and averaging the two would hide the therapeutic-area
gap that was the whole finding. **They are lower than round one's and they mean
more.** Round one measured against the 500 most
frequent distinct strings per field — almost pure head — and scored 96.6% /
92%. Round two measures against every value occurring twice or more plus a
seeded random draw from the singleton tail, then weights each sampled singleton
back up to the corpus rows it stands for. Different question, harder question.

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
`measure`. Almost none of that 23-point drop is lost recall — it is a matching
defect that a coverage number cannot see.

Synonyms were being matched as substrings. `ess` matched inside "assessment"
and "progression", assigning **9.8% of every outcome row in the corpus** to
`epistaxis_severity_score`, a nosebleed instrument used in one rare-disease
indication. `alt` and `ast` matched inside "Health" and "Past" → `liver_enzymes`.
`fa` inside "Fatigue" and "Factor" → `fluorescein_angiography_findings`. `ree`
inside "preeclampsia" and "-free" → `resting_metabolic_rate`. Every one of those
inflated coverage while silently poisoning exactly the same-measurement
comparisons the warehouse exists to support.

The fix is `matching.yaml`, a new file: whole-token matching, case-sensitive
short acronyms, longest-match-wins where a file declares no precedence, and the
field cascade. Matching rules are now part of the vocabulary rather than left to
whoever writes the matcher. Two further defects fell out of writing it down —
"AEs Leading to Discontinuation" resolving to the generic `adverse_event`, and
"Impact of Weight on Quality of Life-Lite" resolving to `body_weight` on the
word "weight" — both fixed by longest-match, with no list to maintain.

### What round two changed

* **`matching.yaml`** — new. The contract above.
* **`forms.yaml`** — `event_free_days` and `correlation` added; a guarded generic
  `<good outcome> rate` pattern (the closed prefix list was catching none of "R0
  Resection Rate", "Sputum Culture Conversion Rate", "MRD Negativity Rate");
  `not_if_matches` so physiological rates stay out of it; bare-acronym patterns
  (OS, PFS, ORR, pCR…) matched case-sensitively. 63.9% from `measure`, up from
  58.6%.
* **`timepoint_patterns.yaml`** — 88.4% → 93.1%. Parenthetical dropping,
  hyphenated units, ordinal timepoints, minutes, an open-ended anchor, intervals
  between two non-baseline timepoints. And one **reversal**: see below.
* **`measurements.yaml`** — 12 terms added, each from a repeated miss: the
  standard neurocognitive battery (HVLT-R, BVMT-R, TMT, COWAT, digit symbol),
  `surgical_margin_status`, `pathological_response`,
  `biochemical_marker_normalisation`, `disease_progression_event`, and others.
* **`ta_mesh_mapping.yaml`** — 12 disagreements found against 80 real MeSH
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
`baseline_to_timepoint`. "up to" is decisive at 79%; "through" is a coin flip
and genuinely ambiguous in the source — the same corpus has "Baseline through
Week 52" against "Change from Baseline through Week 216" *and* against
"Annualized asthma exacerbation rate over 52 weeks". The connective does not
carry the distinction. The form does, so `timepoint_patterns.yaml` now has a
`disambiguation` block that hands the call to the form dimension instead of
guessing.

### The cascade, and one rule that is deliberately absent

Reading `description` when `measure` names no form recovers 36.1% of the
otherwise-unmatched rows — 140 of 1,000. That is why `matching.yaml` specifies a
field cascade, and why a cascade hit is recorded as `syntactic_rule`, never
`exact`.

It is also why there is **no rule mapping a bare "Adverse Events" title to
`incidence_proportion`**, tempting as that is at ~380 estimated corpus rows. Of
the eight bare safety-titled outcomes in the joined export, five have a
description that says "incidence and severity of adverse events" outright. The
registry answers the question itself in the majority of cases; where it does
not, `not_stated` is honest and a lexical guess is not.

References are the mirror image: 17.4% of rows from `measure`, **44.2% from
`time_frame`**. The time origin of an endpoint is written into the timing field,
not the title, so the reference cascade reads `time_frame` first.

## What round three changed: the vocabulary was oncology-shaped

Rounds one and two were measured against samples drawn without regard to
therapeutic area, and the vocabulary they produced was written the same way --
which, given where the reference tables and the reviewers' attention came from,
meant oncology. Round three is the first round measured on a corpus that is not:
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
of registry text. Cardiovascular endpoints are, if anything, *more* stereotyped
than oncology ones -- the same adjudicated event list recurs trial after trial.
The gap was the library.

The same bias sat in the event axis, where being wrong is louder. Event
resolution on event-family rows ran **72.0% in oncology against 48.8%
cardiovascular and 47.7% respiratory**; it is now **73.2% / 72.9% / 59.2%**. An
event-family row whose event does not resolve renders at `partial` tier, which
is honest. One that resolves to the *wrong* event renders a confident,
standards-conformant, clinically wrong sentence -- the disease
`docs/EVENT_SEMANTICS_SPEC.md` was written to cure, and it was alive and well
outside oncology:

* `Major adverse cardiovascular events within 30 days (MACE30)` resolved its
  event to `death_any_cause`, and `Major adverse cardiovascular event (MACE)` to
  `stroke` -- both off the components spelled out in the description, because
  the event cascade reads `description` when `measure` yields nothing and MACE
  had no event of its own to yield.
* An endpoint counting **cardiovascular** death resolved to `death_any_cause`,
  the strictly wider event, for want of a narrower term.

### What was added

* **`measurements.yaml`: 69 terms** (165 → 234), 40 cardiovascular and 29
  respiratory, every one of them from a repeated miss in the review queue rather
  than from a textbook. The cardiovascular block leads with the adjudicated
  events -- infarction, stroke, bleeding, revascularisation, stent thrombosis,
  VTE, atrial fibrillation, arrhythmia, cardiac arrest, unstable angina, limb
  events, LVAD/transplant, worsening heart failure -- then the quantities
  (blood pressure unspecified, MAP, LV volume and mass, strain, diastolic
  function, troponin, infarct size, cardiac output, the four missing lipid
  fractions, KCCQ/MLHFQ/SAQ, peak VO2, platelet reactivity, arterial stiffness,
  endothelial function, decongestion) and the serum chemistry every diuretic and
  RAAS trial reports. The respiratory block covers what spirometry misses (PEF,
  static lung volumes, oscillometry, LCI), the type-2 biomarkers (FeNO, blood
  and sputum eosinophils), the PRO instruments that dominate COPD and asthma
  (CAT, E-RS, ACT, the symptom diaries, mMRC, TDI, cough, K-BILD), sweat
  chloride, corticosteroid-sparing, respiratory support and blood gases -- plus
  the right-heart-catheter variables of pulmonary hypertension, which
  `ta_mesh_mapping.yaml` deliberately routes to `respiratory`.
* **`events.yaml`: 15 terms** (23 → 38) -- the cardiovascular event classes,
  `cardiovascular_death`, the MACE composite, respiratory support escalation
  and failure, and the two benefit events (`hospital_discharge`,
  `ventilator_weaning`) the direction diff below forced. Per this file's
  cross-file synonym discipline the wording lives on the measurement and the
  event is reached through `implies_event`; the exceptions are argued in
  place.
* **`scales.yaml`: 8 units** (59 → 67) -- L/min, m/s, kPa, Wood units,
  dyn·s·cm⁻⁵, µmol/L, ppb, mg/day.
* **`named_endpoints.yaml`: 2 definitions** -- `ttcw` (time to clinical
  worsening, the pulmonary-hypertension analogue of PFS) and `daoh`.
* **Three existing measurements were wired to events that already existed** and
  had simply never been pointed at: `hospitalisation` → `hospitalisation_event`
  (279 rows), `treatment_discontinuation`, `intracranial_haemorrhage` →
  `bleeding_event`. One line each, and between them a third of the
  non-cardiovascular event-coverage gain above.
* **`directions.yaml`** gained two cardio-respiratory harm cues, as the
  documented fallback for rows whose event does not resolve.

Of 234 measurement terms, **exactly one (`graves_orbitopathy_qol`) fails to fire
anywhere in this pull** -- which also retires round two's "`fev1`, `easi`,
`madrs`, `edss`, `womac` and 20 other terms have never fired on any sample"
caveat. FEV1 alone carries 727 rows here.

### MACE is a measurement, not a named endpoint

The tempting move was to give MACE a `named_endpoints.yaml` definition, the way
PFS has one. It is the wrong shape. A named-endpoint definition pins a *form*,
and MACE is as often an incidence proportion as a time-to-first-event in this
corpus -- pinning `time_to_event` would be exactly the lexical guess the cascade
section below refuses to make for bare "Adverse Events". MACE stays a
measurement whose `implies_event` reaches a MACE event, which also keeps the
`SAME_MEASUREMENT_DIFFERENT_FORM` join between "incidence of MACE" and "time to
first MACE" that this warehouse exists to build. All 97 event-family MACE rows
in this corpus now resolve to the MACE event; before round three, none did.

### Five over-matching synonyms, caught by diffing against the previous run

Round two's lesson was that a coverage number cannot see a matching defect. So
round three was measured the other way as well: every row conformed *before*
these terms was re-conformed after, and the ~1,460 that changed measurement were
read, along with every row whose DIRECTION changed while its measurement did
not. Most were fixes -- "Transient ischemic attack" leaving `disease_exacerbation`
(it had matched the synonym "attack"), "Lung Clearance Index" leaving
`pk_clearance`, "Major bleeding" leaving `haemoglobin`. Five were defects in the
new terms, and all five are the same failure mode as round two's `ess`:

| synonym | what it also matched | fix |
|---|---|---|
| `Raw` (airway resistance) | "raw score", in 8 PROMIS/rating-scale descriptions | dropped; `sRaw`/`siRaw` kept |
| `GCS` (global circumferential strain) | Glasgow Coma Scale, 4 of 5 occurrences | dropped, spelled out |
| `NCS` (nasal congestion score) | "Abnormal Not Clinically Significant (NCS)", 8 of 25 | dropped, spelled out |
| `RHI` (reactive hyperaemia index) | Robarts Histopathology Index, 4 of 14 | dropped, spelled out |
| `exercise capacity` (peak VO2) | 10 rows that said only "Functional Exercise Capacity" | dropped: it names the shared *concept*, not this instrument |

Two more were caught before they shipped, from the corpus rather than from a
diff: `creatinine` is longer than both `UACR` and `eGFR` and so took
"urine albumin-to-creatinine ratio" and "eGFR (CKD-EPI creatinine equation)"
rows off them, fixed with two `not_if_matches` vetoes; and bare `sodium` /
`potassium` are the commonest drug-salt suffix in pharmacology (`amogammadex
sodium`, `sodium zirconium cyclosilicate`), so both are matched only with a
`serum`/`plasma`/`urinary` qualifier.

The direction half of that diff found the one defect a measurement diff cannot
show. Wiring `hospitalisation` to `hospitalisation_event` (polarity `harm`) is
right for an admission endpoint and wrong for "Time to discharge", which flipped
from `shorter_is_better` to `longer_is_better` -- because direction is
event-first, and the discharge had no event of its own to be first with. The fix
is two benefit events, `hospital_discharge` and `ventilator_weaning`, not a
retreat from the wiring: `directions.yaml` has listed "time to ... discharge"
and "... extubation" among its benefit cues since it was written, so this is the
same judgment moved to the layer that now decides ahead of the cues.

Three acronyms were deliberately left out on the same evidence and are recorded
in their terms' notes: `PE` (physical examination and plasma exchange as well as
pulmonary embolism), `BDI` (the Beck Depression Inventory, in 54 of 56
occurrences, not the Baseline Dyspnea Index) and `PCI` (as often the index
procedure a trial enrols after as the outcome event). `MI`, `MALE`, `CAT`, `ACT`
and `MAP` were kept but carry either a case-sensitive pattern, a
`not_if_matches` veto, or both.

## Known gaps

* ~~**Live AACT/CT.gov access is still unavailable from this build sandbox.**~~
  **Partly closed.** Round three was measured against a real pull's export --
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
  `exact` 966 / `syntactic_rule` 323, with 3,695 unresolved -- 63.6% resolved
  overall, and the per-area split under "What round three changed" above.
  `endpoints usdm coverage` over the same corpus moves from templated 33.0% /
  partial 21.7% / verbatim 45.3% before round three to **38.0% / 24.8% /
  37.2%** after, with `reference defaulted` steady at 4.3% → 4.5% of templated.
  Two caveats on those figures. The pull's export carried no
  `raw.studies.allocation`, so the randomisation gate on a named-endpoint
  definition's `reference` never opened -- every PFS/OS/TTCW row here resolved
  its reference through the ordinary cascade, and a pull that carries
  allocation should show more templated rows and a different defaulting rate.
  And phase D's own question -- whether `incidence_proportion`, `event_count`
  and `event_rate` should switch from `{measurement}` to `{event}` -- is now
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
  demonstrably thin — the same "absence of evidence either way" this bullet
  described, now narrowed to the areas the pull did not reach.
* **`adverse_event` and `serious_adverse_event` have no event term**, and
  between them account for 1,893 event-family rows carrying
  `event: not_stated` — by a wide margin the largest remaining event-coverage
  gap, and the reason respiratory event resolution (59.2%) still trails
  cardiovascular (72.9%). Round three did not close it deliberately: an
  adverse-event event term changes the projection for every therapeutic area at
  once, which is a corpus-wide decision and not a cardio-respiratory one. It is
  the next single highest-value edit in this file.
* **Composite endpoints that name a union in free text still resolve to one
  member.** "Time from randomisation to first occurrence of arterial thrombosis,
  venous thromboembolism or cardiovascular death" gets `venous_thromboembolism`
  by longest match, which is defensible and lossy. MACE is handled because it
  has a NAME; unions written out in full are not, and
  `docs/COMPOSITE_ENDPOINTS_SPEC.md` is where that belongs rather than here.
* **The residual 6.9% of unclassified timepoints is genuine long tail** —
  multi-phase narrative schedules, per-cohort schedules, labelled study periods
  with no absolute horizon. Chasing it would overfit.
* **Thresholds are parsed, not vocabularised** (per the plan). The comparator/
  value/unit regexes live with the step-3 parser; `forms.yaml` only flags which
  forms expect one via `expects_threshold`.

## Adding or changing a term

1. Edit the YAML.
2. `uv run endpoints vocab validate --check-only`.
3. `uv run pytest tests/test_vocab_loader.py` — the reference-table fixtures
   there are the acceptance criteria for step 3, so they should keep passing.

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
is this endpoint", independent of what is measured -- which is a sentence frame
written out:

```yaml
- form: change_from_baseline
  template: "Change from {reference} in {measurement}[ {timepoint}][ ({scale})]"
  reference_fallback: patient_baseline
```

`{tag}` is a slot filled from a conformed endpoint's dimensions; `[ ... ]` is
dropped whole when a tag inside it did not resolve. A tag outside brackets is
required, and a form whose required tags do not resolve renders verbatim rather
than as half a sentence. `vocab validate` rejects a form with neither a
template nor `verbatim: true`, a tag outside the closed set, a template with no
required tag, and a missing `{threshold}` on a form that declares
`expects_threshold`.

The file also carries `purpose_by_domain` (USDM requires `Endpoint.purpose`; no
registry record states one, so it is derived from the measurement's `domain`)
and `objective_templates` (USDM hangs endpoints off objectives; registry
records have none).

### `inline_label`: the sentence-fragment form of a term

A term's `label` is a *display* label, written for a review table. Several read
wrong inside a generated sentence:

```
'First dose / start of treatment'      -> 'the start of treatment'
'Percentage of participants (%)'       -> '%'
'No reference (absolute quantity)'     -> null
```

So `references.yaml`, `scales.yaml` and `measurements.yaml` carry an optional
`inline_label`, and a renderer uses it in preference to `label`. `null` means
the term has no sentence form at all -- `references.yaml`'s `none` and
`not_stated` name the *absence* of a reference, and printing "Change from No
reference (absolute quantity) in FEV1" is worse than dropping the phrase.

`vocab validate` warns where a tag-reachable term has no `inline_label` and a
`label` that will not read as a fragment: one containing a slash, or a
parenthetical that is not a bare abbreviation. `Glycated haemoglobin (HbA1c)`
passes; `Ratio (dimensionless)` does not. That check found 50 terms on its
first run, all now curated.

---

## `events.yaml` and `named_endpoints.yaml`: the event axis

Added by `docs/EVENT_SEMANTICS_SPEC.md`, after a live-pull validation
(NCT01777919) showed the USDM projection rendering "Time from randomisation to
Tumour burden (RECIST)" for a Progression-Free Survival endpoint -- a
confident, standards-conformant, and clinically wrong sentence. The model had
dimensions for the time origin (`reference`) and for what is assessed
(`measurement`), and none for the EVENT a time-to-event endpoint actually
counts down to. `event` is the new ninth dimension; `named_endpoints.yaml` is
what makes recognising a literature name (PFS, OS, DFS...) resolve the whole
bundle -- form, event, reference, measurement -- at once, rather than donating
the name to one dimension as a synonym.

**`measurements.yaml` is unchanged in grain.** PFS and ORR still share
`tumour_burden_recist` -- that shared id is the SAME_MEASUREMENT_DIFFERENT_FORM
join this project exists to build, and regraining it to event level would fix
one sentence by destroying that join. Instead, endpoint-NAME synonyms that used
to live on `tumour_burden_recist` / `vital_status` / `disease_recurrence` /
`treatment_failure` ("progression-free survival", "PFS", "overall survival",
"OS", "disease-free survival", "DFS"...) moved to `named_endpoints.yaml`; each
measurement kept only its assessment-language synonyms (RECIST, tumour
response, mortality-rate phrasings that genuinely name the ascertainment). A
handful of event-shaped measurements (`vital_status`, `disease_recurrence`,
`disease_progression_event`, `treatment_failure`, `disease_exacerbation`) gained
`implies_event`, so a row whose measurement IS the event resolves it with no
duplicated text match -- `events.yaml` deliberately does not repeat a synonym
a measurement's `implies_event` already reaches, since the validator now
rejects a synonym claimed by two of {measurement, event, named_endpoint}.

**Event resolution is a `conform_row` step (0), run before the ordinary
per-dimension cascade**, not a projection-time inference:
`named_endpoints.yaml` match -> `events.yaml` match over `measure` ->
`description` -> the resolved measurement's `implies_event` -> `not_stated`.
It only runs for the six **event-family** forms (`forms.yaml`
`event_family: true`: `time_to_event`, `event_free_rate_at_timepoint`,
`event_free_days`, `incidence_proportion`, `event_count`, `event_rate`) --
declared explicitly rather than derived from `direction_rule`, because
direction does not carve this joint (`event_free_rate_at_timepoint` is
`higher_count_better` yet is entirely about an event; `shift_from_baseline` is
`inherit_event_polarity` yet names none, and stays outside `event_family` on
purpose -- `vocab validate` warns rather than errors on that one case, so it
does not read as a defect).

A `named_endpoints.yaml` definition **fills** the measurement and reference
dimensions only when their own ordinary cascade is silent, and **fills** form
the same way (not an unconditional override -- forms.yaml's own cascade
already resolves a named endpoint's typical form on its own, e.g.
"progression-free survival" via `time_to_event`'s own synonym, and must stay
free to route a *landmark* phrasing like "2-Year Overall Survival" to
`event_free_rate_at_timepoint` instead). A definition's `reference` applies
only when `raw.studies.allocation` says the study is randomised -- asserting
"from randomisation" on a single-arm trial's PFS would be exactly the
unannounced-default disease `docs/USDM_PROJECTION_INTEGRITY_SPEC.md` exists to
cure.

**Direction is event-first.** `directions.yaml`'s `event_polarity_cues`
free-text regexes are now the fallback layer for rows whose event does not
resolve, not the primary evidence: resolution order is the resolved event's
own `polarity`, then the cues, then the matched measurement's
`event_polarity`, then `not_stated`.

**The USDM projection** (`docs/USDM_ENDPOINTS_API_SPEC.md`) gained `{event}`
as an eighth tag -- a `BiomedicalConceptSurrogate` per distinct resolved event,
served at `/v4/vocab/event/{id}`, same shape as `{measurement}`. Three
consequences: `time_to_event` / `event_free_rate_at_timepoint` /
`event_free_days` now render `{event}` instead of `{measurement}`, with
`{reference}` optional rather than defaulted (`reference_fallback` deleted --
a form-keyed fallback asserted randomisation on DoR rows, whose origin is
`response_onset`, and on single-arm trials alike); an event-family row whose
event does not resolve degrades to the `{measurement}[ {timepoint}]` frame at
`partial` tier rather than rendering the assessment into the event's place;
and the measurement surrogate is now minted for every conformed endpoint with
a resolved measurement, tag-referenced or not, so the PFS<->ORR join does not
silently disappear from the document just because the sentence stopped
mentioning `{measurement}` directly. `incidence_proportion` / `event_count` /
`event_rate` deliberately keep `{measurement}` for now -- their corpus is
AE-dominated, where the measurement *is* the event and the current rendering
already reads correctly; switching them waits on measured event coverage
(`docs/EVENT_SEMANTICS_SPEC.md` phase D).
