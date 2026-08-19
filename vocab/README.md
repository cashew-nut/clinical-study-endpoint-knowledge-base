# The vocabularies

Eight YAML files, one per dimension (plus the MeSH mapping), built during
build-order step 2 from a review of 500 Phase 3 studies' `design_outcomes`.
This file records the schema, the judgment calls, and the things a reviewer
should push back on.

```
forms.yaml               16 terms   what kind of number the endpoint is
measurements.yaml       153 terms   what quantity or event it is about
references.yaml          16 terms   what it is measured against
directions.yaml           7 terms   which way is better (derived, not matched)
scales.yaml              57 terms   the unit
therapeutic_areas.yaml   24 terms   the TA, derived from MeSH
timepoint_patterns.yaml  11 terms   time_frame categories + extraction regexes
ta_mesh_mapping.yaml                MeSH condition/intervention -> TA
```

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

* "Baseline **through** Week 52" reads as a collection window; "Baseline **to**
  Week 52" reads as an assessment at a horizon. Where the conformed form
  disagrees, trust the form and record the conflict.
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

## Coverage, honestly

Measured against the 500-study `vocab_review.csv` sample:

| | coverage |
|---|---|
| `time_frame` classified to a timepoint category | 96.6% of rows |
| `measure` matched to a measurement | 92% of rows |
| `measure` matched to an explicit form | 69% of rows (the rest are `not_stated`) |

Three caveats on those numbers (the first two were closed by the session that
resolved `docs/NEXT_SESSION.md`'s gaps -- see below):

1. **The sample itself was truncated.** `vocab sample` used to keep the 500 most
   frequent distinct values per field, alphabetically-truncated below that. It
   now keeps every value occurring `--min-frequency` times or more uncapped, plus
   a seeded random sample of the tail (`--singleton-sample`), and reports
   per-field coverage (`_coverage.csv`) instead of leaving it invisible. A
   `--format rows` export also makes measure/time_frame/description jointly
   reviewable. This sandbox cannot reach either ingestion backend to re-run the
   500-study sample for real updated numbers (see below); `endpoints vocab
   sample` should be re-run against a real pull before the next vocabulary
   review round, and this section's numbers updated from its `_coverage.csv`.
2. **Exact-match coverage of `measure` will never approach 100%,** because the
   strings are close to unique per trial. That is the designed shape: syntactic
   match, then semantic fallback, then review queue.
3. **The MeSH tree numbers are still effectively unvalidated against AACT.**
   Per the AACT data dictionary checked for this project, `ctgov.mesh_terms` has
   zero rows in the live database today, independent of its column names -- so
   there is no tree-number join available from AACT regardless. `tree_prefixes`
   now also fires from the CT.gov API backend's coarse `browseBranches`
   abbreviation (one tree letter per study, not per condition), and
   `endpoints ta diff-tree` (task 3's diff tool) is implemented and tested, but
   has only run against synthetic data in this sandbox -- see
   `ta_mesh_mapping.yaml`'s `caveats` block for two illustrative disagreements
   it already surfaced against the real tree_prefixes/term_patterns tables, and
   run it for real once a live pull is possible.

## Known gaps

* **Live AACT/CT.gov access is still unavailable from this build sandbox** --
  confirmed this session (both hosts return HTTP 403 on the outbound proxy), not
  just suspected. Everything below the vocabulary layer (`pull --ta`, the TA
  resolver, `endpoints ta diff-tree`) is implemented and unit-tested against
  fakes/synthetic data, but has never run against a real pull. That's the next
  session's first task once network access is available.
* **Rheumatology and respiratory are thin in this sample.** ACR20 and FEV1 are
  in the vocabulary because the plan's reference table names them, not because
  the 500 studies exercised them. A rheumatology-weighted pull would be the way
  to check that half of the vocabulary.
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
