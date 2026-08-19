# The vocabularies

One YAML file per dimension, plus the MeSH mapping and the matching contract.
Built during build-order step 2 and revised in vocabulary review round two
against an unbiased sample of 13,542 outcome rows. This file records the schema,
the judgment calls, and the things a reviewer should push back on.

```
forms.yaml               18 terms   what kind of number the endpoint is
measurements.yaml       165 terms   what quantity or event it is about
references.yaml          16 terms   what it is measured against
directions.yaml           7 terms   which way is better (derived, not matched)
scales.yaml              59 terms   the unit
therapeutic_areas.yaml   24 terms   the TA, derived from MeSH
timepoint_patterns.yaml  11 terms   time_frame categories + extraction regexes
ta_mesh_mapping.yaml                MeSH condition/intervention -> TA
matching.yaml                       HOW all of the above are matched
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
`skin_disease_global_severity`. The graph layer can build
`SAME_MEASUREMENT_DIFFERENT_FORM` from `id` and a `SAME_CONCEPT` edge from
`concept` with no extra vocabulary. If concept later needs its own definitions
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

These are round-two numbers, measured on the unbiased sample. **They are lower
than round one's and they mean more.** Round one measured against the 500 most
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
inflated coverage while silently poisoning exactly the `SAME_MEASUREMENT` edges
the graph layer exists to build.

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

## Known gaps

* **Live AACT/CT.gov access is still unavailable from this build sandbox.**
  Everything below the vocabulary layer (`pull --ta`, the TA resolver,
  `endpoints ta diff-tree`) is implemented and unit-tested against fakes, but has
  never run against a real pull.
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
* **Rheumatology and respiratory are still thin.** ACR20 and FEV1 are in the
  vocabulary because the plan's reference table names them, not because the
  corpus exercised them. `fev1`, `easi`, `madrs`, `edss`, `womac` and 20 other
  terms have never fired on any sample. That is not evidence they are wrong — it
  is an absence of evidence either way, and a therapeutic-area-weighted pull is
  the way to close it.
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
