# Roadmap: correlating conformed endpoints with public results data

> **Status: proposal.** A roadmap rather than a `_SPEC.md` design of record --
> it names candidate deliverables and their trade-offs; whichever are chosen get
> their own spec. Nothing here exists in code. There is no `raw.outcome_*`
> table, no `conformed.endpoint_results`, no dispersion normaliser, no
> `endpoints stats` command. This is a menu of deliverables to schedule or
> decide against, written after a research pass over what public data could
> reasonably be joined to the warehouse. Effort sizes are relative, not
> estimates. Several items are gated on a measurement that needs a live pull --
> see [Recommended sequence, with a gate](#recommended-sequence-with-a-gate).

Read alongside:

* [`../vocab/README.md`](../vocab/README.md), "Coverage, honestly" -- the
  measured limits of the join key everything below depends on
* [`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md), "Cross-study comparability" --
  the questions the warehouse can already answer, and its caveat
* [`COMPOSITE_ENDPOINTS_SPEC.md`](COMPOSITE_ENDPOINTS_SPEC.md) -- the other
  open proposal, and the reason this one is also written as a gate rather than
  a plan

---

## The thesis in one paragraph

`conformed.endpoints` is not primarily a description of endpoints. It is a
**join key**. Three sponsors writing `"PFS"`, `"Progression free survival per
RECIST 1.1"` and `"Time from randomization to documented progression or
death"` currently share nothing a machine can group on; after conforming they
share `measurement_id`, `form_id`, `reference_id`, `event_id`, `scale_id` and a
classified timepoint. Everything worth building next is an answer to one
question: *now that rows can be grouped, what is worth attaching to the group?*
The most valuable thing to attach is the one thing this project pointedly does
not yet ingest -- what the trials actually found.

## What is already true, and what it costs

Three facts constrain every deliverable below.

**The pipeline ingests protocol text, not results.** `raw.design_outcomes`
carries `measure`, `time_frame`, `description`, `population` -- the *planned*
endpoint. Nothing in `raw.*` holds a number a trial reported.

**But the results are already coming down the wire.** `ingest/ctgov_api.py`
`_fetch_page` sends `query.term`, `pageSize` and `sort` and no `fields`
parameter, so the API returns the full study record. For any study with
`hasResults`, that record already contains `resultsSection` --
`outcomeMeasuresModule`, `baselineCharacteristicsModule`,
`participantFlowModule`, `adverseEventsModule` -- and
`ingest/ctgov_api.py`'s row builders simply never look at it. The AACT backend
is the same story from the other side: `run_pull` reads `ctgov.design_outcomes`
and not `ctgov.outcomes`, `ctgov.outcome_measurements`,
`ctgov.outcome_analyses` or `ctgov.baseline_measurements`, all of which sit in
the same attached database. **The expensive part of results ingestion -- the
network -- is already paid for.** What is missing is parsing and landing.

**The join key covers about two thirds of rows, head-weighted.** Measurement
coverage is 64.5% on the unbiased sample; 77% of `measure` strings occur
exactly once. Every aggregate below is therefore computed over a biased subset
that over-represents OS, PFS, ORR and adverse-event counts, and
under-represents the instrument used in two trials. This is not a reason not to
build -- it is a reason every deliverable must carry its own denominator, the
way the cheat sheet's comparability queries already do.

---

## Tier 0 -- deliverables available today, with no new data

Worth listing first because they need no ingestion work at all, and because
they are the cheapest way to find out whether the conformed corpus is dense
enough to support Tier 1.

### D1. Endpoint co-occurrence and study-design fingerprints

Which endpoints travel together within a study; the typical
primary/secondary/other structure per therapeutic area and phase; how many
endpoints a trial declares as a function of phase, TA and design. All of it is
`GROUP BY` over `conformed.endpoints` joined to `raw.studies`.

*Why it matters:* "number of endpoints" and "protocol complexity" are among the
features the trial-success prediction literature leans on hardest, and they are
currently extracted by everyone from raw strings. A conformed co-occurrence
matrix is a better feature set than a count, and it is free.

*Ships as:* a `conformed.endpoint_cooccurrence` view plus a cheat-sheet
section. Small.

### D2. Temporal drift in endpoint choice

`raw.studies.start_date` is already landed. Group conformed endpoints by
start-year × TA × form/measurement and you get the adoption curve of every
endpoint in the vocabulary: PFS displacing OS, the arrival of PRO instruments,
the rise and fall of a specific responder threshold.

*Why it matters:* it is the single most legible output the corpus can produce
today, it needs no new source, and it validates the vocabulary in public --
drift curves that match known practice are evidence the conforming works.

*Ships as:* a query set and one chart. Small.

### D3. Threshold and responder-definition dispersion

`threshold_comparator` / `threshold_value` / `threshold_unit` are already
parsed. For a given measurement, what thresholds do sponsors actually use --
PASI 75 vs 90 vs 100, ≥50% reduction vs ≥30%? Where a measurement has more than
one threshold in play, that is a live comparability hazard that no one has
tabulated.

*Why it matters:* it is a direct answer to "what can be correlated now that we
can group", and it is the first place where grouping reveals a disagreement
between sponsors rather than a synonym.

*Ships as:* a query set. Small.

---

## Tier 1 -- the results section: same source, new tables

This is where the standard-deviation question lives. The tier is one
prerequisite (D4-D5) followed by four independent payoffs.

### D4. Land the results section (`raw.outcome_*`)

Parse `resultsSection` from the payload already fetched, and the corresponding
AACT tables, into a source-agnostic shape mirroring what the project already
does for protocol data:

| table | grain | carries |
|---|---|---|
| `raw.outcome_measures` | one reported outcome | title, description, time frame, `param_type`, `dispersion_type`, `unit_of_measure`, type (primary/secondary/other) |
| `raw.outcome_groups` | one arm within one outcome | group title, description, `n` |
| `raw.outcome_measurements` | one arm × category × class | `param_value`, `dispersion_value`, `dispersion_lower_limit`, `dispersion_upper_limit` |
| `raw.outcome_analyses` | one statistical comparison | groups compared, `p_value`, `param_type` (HR/OR/RR/mean difference), `param_value`, CI percent and limits, method, non-inferiority type and margin |
| `raw.baseline_measurements` | one baseline characteristic × arm | title, units, `param_type`, `param_value`, `dispersion_type`, `dispersion_value` |

Two design points carry over from the existing ingest layer and should not be
relitigated: both backends land the same shape, and `ensure_table` reconciles
the schema rather than dropping and refetching.

*Risk to size before committing:* the results-section outcome title is a
**different string** from the protocol-section `measure`. They are usually the
same and sometimes not -- sponsors reword, split one planned outcome into
several reported ones, or report outcomes never registered. Linking results
rows back to planned rows is therefore its own matching problem, not a foreign
key. Measure the exact-match rate on a real pull before designing around it.

*Ships as:* two backend parsers, five DDLs, upsert wiring. Medium, and it gates
everything else in this tier.

### D5. Link results to the vocabulary (`conformed.endpoint_results`)

Do not write a second matcher. Run the *existing* conformance engine over
results-section titles and descriptions -- they are the same kind of free text
the engine already handles -- and record the link with its own provenance:
exact string match to the planned outcome, conformed-to-the-same-vocabulary
match, or unlinked.

*Why this shape:* it reuses `conform/` unchanged, it keeps the audit trail the
project already insists on (`match_method`, `source_field`, `confidence`), and
an unlinked results row goes to a review queue rather than being force-joined.

*Ships as:* a link table, a `conform --results` path, a review reason. Medium.

### D6. The dispersion normaliser (`conformed.endpoint_dispersion`)

The heart of the standard-deviation question, and the part most likely to be
done wrong. Reported spread on ClinicalTrials.gov is **not** a standard
deviation; `dispersion_type` is a small closed set that mixes standard
deviation, standard error, inter-quartile range, full range, several confidence
interval widths, and geometric coefficient of variation. `param_type` likewise
mixes mean, median, least-squares mean, geometric mean and several count types.
Pooling them without conversion produces a number that means nothing.

The deliverable is a normaliser that emits, per arm-level measurement, a single
`sd_estimate` plus the path that produced it:

| reported as | conversion | note |
|---|---|---|
| standard deviation | identity | |
| standard error | `SE × √n` | needs a trustworthy arm `n` |
| *k*% confidence interval | `(upper − lower) × √n / (2 × z_k)` | z from the stated percent, not assumed 95% |
| inter-quartile range | Wan et al. (2014) estimator | median-based; flag as approximate |
| full range | Wan et al. (2014) estimator | weak; flag, and consider excluding |
| geometric CV | log-scale only | do not silently mix with arithmetic SD |

Every row records `sd_method`, `sd_is_derived` and the inputs used, exactly as
the conformance pipeline records how each dimension was decided. A derived SD
must be filterable, because a library built mostly out of range-derived
estimates is a different object from one built out of reported SDs.

**`scales.yaml` is a prerequisite, and it was built for this.** The file
already carries `kind`, `si_equivalent` and `factor_to_si` -- 21 of 59 terms
have a conversion factor. Pooling FEV1 in L with FEV1 in mL, or HbA1c in % with
mmol/mol, is exactly the case that comment anticipated. Completing
`factor_to_si` coverage for the kinds that appear in results units, and adding
a unit-string normaliser for the free-text `unit_of_measure` field, is part of
this deliverable rather than a follow-on.

*Ships as:* a normaliser module, a vocabulary round on scales, a table. Medium,
and it is the piece where an error is least visible downstream.

### D7. The endpoint statistics reference -- `endpoints stats` *(flagship)*

The user-facing answer to "what standard deviation should I expect for this
endpoint": given a measurement (optionally narrowed by form, scale, timepoint,
therapeutic area, phase and population), return the empirical distribution of
arm-level variability across every trial that reported it.

```
$ endpoints stats --measurement fev1 --form change_from_baseline --scale litres

FEV1, change from baseline, litres
  studies      41      arms   96      participants   18,204
  SD           median 0.34   IQR 0.28-0.41   n=96 arms
               reported 71 · derived from SE 19 · from 95% CI 6
  timepoints   week_12 (22 studies)  week_24 (13)  week_52 (6)
  coverage     41 of 63 conformed studies reported a usable dispersion (65%)
```

The coverage line is not decoration. It is the same discipline the cheat sheet
applies to comparability queries, and without it the command is a machine for
producing confident numbers off eight arms.

*Why it matters:* this is the deliverable that turns the corpus from a
descriptive resource into a design tool. Nobody publishes an empirical prior
for the variability of a given endpoint at a given timepoint; every
statistician assembling a sample-size calculation reconstructs it by hand from
two or three papers they happen to know.

*Ships as:* one CLI command, one API endpoint, one documented caveat block.
Medium, and it is the reason to do D4-D6.

### D8. Baseline variability, as a second and larger denominator

`baselineCharacteristicsModule` reports mean ± SD for baseline characteristics
across far more trials than report a usable outcome dispersion, and for
continuous outcomes methodologists generally prefer the **baseline** SD to the
follow-up SD when standardising. For a change-from-baseline endpoint it is also
the more defensible planning input: the SD of a change score depends on the
correlation between baseline and follow-up, which registries never report, so a
change-score SD cannot be converted to a raw SD or vice versa without an
assumption you would have to invent.

Treating baseline variability as its own deliverable rather than a fallback
inside D7 keeps that distinction visible in the output instead of burying it.

*Ships as:* a parallel table and a `--source baseline` flag on `stats`. Small
once D4 lands.

### D9. Effect sizes, p-values and non-inferiority margins

From `raw.outcome_analyses`, grouped by the conformed endpoint: the empirical
distribution of hazard ratios, odds ratios, risk ratios and mean differences;
the distribution of reported p-values; and -- the scarcest of the three --
**the distribution of non-inferiority margins actually used per endpoint**.

*Why it matters:* NI margin selection is currently justified by citing
precedent trials found by hand. A table of every NI margin used for a given
endpoint, with the trials behind it, does not exist publicly in any form I
could find. The p-value distribution is separately interesting as a
reporting-integrity check: prior work found significant results in roughly 60%
of trials posting a treatment effect or p-value, and a per-endpoint version of
that statistic is a finer instrument than a corpus-wide one.

*Ships as:* a query set plus `stats --analyses`. Small once D4 lands.

### D10. Power archaeology

Join `raw.studies.enrollment_count` × the D6 SD estimate × the D9 observed
effect and back out the implied power of trials that ran, per endpoint. Answers
"for this endpoint, how large were the trials that detected a difference, and
how large were the ones that did not".

*Caveat that must ship with it:* this reconstructs *achieved* power from
observed effects, which is not the same quantity as the design power a protocol
assumed, and post-hoc power computed from an observed effect is a well-known
statistical trap. Frame the output as a descriptive distribution of
(N, effect, variability) triples, not as "this trial was underpowered".

*Ships as:* a query set. Small, with a large documentation burden.

---

## Tier 2 -- external sources worth joining

Each of these is an independent, self-contained enrichment. None of them
depends on Tier 1. All are public, and all are keyed on something the warehouse
already has.

### D11. FDA surrogate endpoint table

FDA publishes, under a 21st Century Cures Act requirement and updated roughly
every six months, a table of surrogate endpoints that were the basis of drug
approval or licensure, by disease, distinguishing accelerated from traditional
approval.

*The join:* disease → the existing therapeutic-area resolution; endpoint →
`measurement_id`. The output is a `regulatory_status` cross-reference marking
which measurements in the vocabulary are FDA-recognised surrogates, for which
indication, under which pathway.

*Why it matters:* it is the highest-credibility external label available for an
endpoint, it enriches the *vocabulary* rather than the results, and it makes a
new class of question answerable -- how quickly does a newly-recognised
surrogate propagate into registered trials, and which trials use a surrogate
FDA has never accepted for that indication. Small, high value.

### D12. FDA Clinical Outcome Assessment Compendium

A public, tabular collation of the COAs appearing in approved drug labelling,
organised by therapeutic area. Same join shape as D11, different label: not
"accepted as a surrogate" but "appeared in a label".

*Caveat to carry through:* FDA is explicit that inclusion is neither an
endorsement nor guidance. The derived flag must say "appears in the COA
Compendium", never "FDA-approved endpoint". Small.

### D13. COMET core outcome sets

The COMET Initiative maintains a public database of core outcome sets -- the
minimum set of outcomes that should be measured in all trials of a given
condition. Joining it to the vocabulary yields a per-condition list of "core"
measurements, and therefore a computable **core-outcome adherence rate** per
study and per sponsor.

*Why it matters:* core-outcome adherence is measured today by manual review of
a few dozen trials at a time. The conformed corpus makes it computable across
thousands, which is a genuinely publishable meta-research result rather than an
internal metric. Medium, and access terms should be checked before design --
the database is free to search but I could not confirm a bulk or API export.

### D14. Publication linkage, and registered-vs-published outcome switching

Link each NCT to its publications via Europe PMC's REST service, then conform
the *published* primary endpoint and compare it to the *registered* primary
endpoint.

*Why it matters:* outcome switching -- a primary endpoint changing between
registration and publication -- is one of the best-studied problems in trial
reporting and one of the most labour-intensive to detect, because it requires
judging whether two differently-worded endpoints are the same endpoint. That
judgment is precisely what the conformance engine automates. This is the
deliverable where the project's core competence is most directly load-bearing,
and the one most likely to be cited.

*Secondary benefit:* publications carry results for the roughly 30% of
applicable trials with nothing posted on the registry, which partly repairs the
selection bias described below. Large.

### D15. Trial-outcome labels (CTO / CTOD)

A 2026 *Nature Health* resource publishes success/failure labels for ~125k drug
and biologic trials, assembled from publications, phase transitions and market
signals, with a manually annotated recent subset. It is downloadable and keyed
on NCT ID.

*The join:* endpoint choice → trial success. "Which endpoints are associated
with trials that advanced" is a question the trial-design ML literature asks
constantly using raw string features; asking it over a conformed vocabulary is
a straightforwardly better version of the same question.

*Caveat:* these are weak labels with their own error model, and success is
confounded by indication, sponsor and era. Treat as an external label to
correlate against, never as ground truth. Small to join, large to interpret
responsibly.

### D16. A second registry (EU CTR / CTIS)

EU trials post summary results to CTIS, with public search and per-trial
download. Registration in both registries is common for larger trials.

*Why it matters, and why it is last:* the value is not more trials, it is
**the same trial described twice**. A trial registered in both places gives two
independent free-text renderings of the same endpoint, which is the only clean
test set the conformance engine can ever get for the question "do two different
strings for one endpoint conform to the same vocabulary term?" That is worth
more to this project than the additional rows. Access is per-trial download
rather than bulk, so scale carefully. Medium to large.

---

## The statistics, honestly

An "expected standard deviation for an endpoint" is a **design prior assembled
from observational aggregates**, not an estimate of a population parameter.
Six specific things break it, and each needs to be visible in the output rather
than in a footnote.

**Selection into the results database is not random.** Roughly 70% of trials
that clearly fall under mandatory reporting have results posted; industry
sponsors comply substantially better than academic ones. An SD library built
from posted results is therefore weighted toward industry trials, which differ
systematically in population, monitoring intensity and endpoint choice.

**Conformance bias compounds it.** 64.5% measurement coverage, head-weighted.
The trials whose endpoints conform and the trials that post results are not
independent samples, and the intersection is narrower than either.

**Dispersion type is not standard deviation.** See D6. A pooled number computed
across mixed dispersion types is not wrong by a little.

**Units are free text on the results side.** `unit_of_measure` is
sponsor-written. Without the `scales.yaml` normalisation in D6, pooling silently
mixes L with mL.

**Timepoint and population are part of the endpoint.** The SD of FEV1 change at
week 12 is not the SD at week 52; the SD in a severe-disease enrichment
population is not the SD in a broad one. The vocabulary has a timepoint axis and
`population` is carried through as raw text -- the timepoint must be a grouping
key in D7, and the absence of a structured population axis must be stated as a
limitation rather than papered over.

**Change-score SD and raw SD are different quantities**, and converting between
them requires the baseline/follow-up correlation, which registries do not
report. Never mix them in one pool. This is why D8 exists as its own
deliverable.

The honest framing for D7's output: *"across N trials that reported a usable
dispersion for this endpoint at this timepoint, arm-level SD had median X and
interquartile range Y-Z; here is the coverage, and here are the trials."* Every
one of those clauses is load-bearing.

---

## What I would not build

* **A generic correlation engine.** The value is in a small number of
  well-specified joins, each with its own caveats. A tool that correlates
  everything with everything produces mostly spurious associations over a
  corpus this heterogeneous, and the composite-endpoints spec already records
  why generic layers lose to typed columns here.
* **Meta-analytic pooled effect estimates.** Computing a pooled treatment
  effect across trials grouped only by conformed endpoint means pooling across
  different populations, comparators and eras. That is a systematic review, not
  a query, and shipping it as a query invites exactly the misuse the project's
  audit trail exists to prevent. Distributions, yes; pooled estimates, no.
* **Imputing dispersion where none was reported.** Leave the gap and report the
  denominator.
* **Any individual-participant-data pathway.** Vivli, YODA and the rest are
  request-gated and would break the property that the whole pipeline runs from
  public sources with no agreements.
* **A results-specific matcher.** If results titles need matching, they need
  the existing engine and the existing vocabulary, or the two halves of the
  warehouse stop meaning the same thing.

---

## Recommended sequence, with a gate

**Now, ungated:** D1, D2, D3 (Tier 0) and D11, D12 (the two FDA tables). None
needs new ingestion; together they demonstrate the corpus's value and enrich
the vocabulary while the results work is scoped.

**The gate:** one live pull with results parsing stubbed in, measuring four
numbers before any of Tier 1 is designed in full:

1. What share of conformed studies have `hasResults`.
2. What share of results-section outcome titles match the planned `measure`
   string exactly -- and what the conformance engine does with the rest.
3. The observed distribution of `dispersion_type` and `param_type`, and the
   exact value sets both fields use. The enumerations assumed in D6 above are
   from memory and secondary sources; ClinicalTrials.gov was unreachable from
   the environment this was written in, so they must be confirmed against real
   payloads, not trusted.
4. The share of results rows whose `unit_of_measure` normalises against
   `scales.yaml` as it stands today.

**If the gate passes:** D4 → D5 → D6 → D7, in that order, with D8, D9 and D10
following in any order once D4 lands.

**Independently, whenever:** D13 (COMET) and D14 (publication linkage). D14 is
the largest single item here and the one with the most external upside; it is
listed late because it deserves its own spec, not because it ranks low.

**Last:** D15 and D16.

---

## Standing constraints

Decisions a future change should not quietly undo.

* **Results ingestion stays a thin fetch-and-land**, source-agnostic across
  both backends, exactly as protocol ingestion is. No conforming inside the
  ingest layer.
* **Results text conforms through the existing engine.** No second matcher, no
  second vocabulary.
* **Every derived statistic records how it was derived.** `sd_method`,
  `sd_is_derived` and the inputs are as non-negotiable as `match_method` and
  `confidence` are on the conformance side.
* **Every aggregate ships its denominator.** A `stats` output without a
  coverage line is a defect, not a terse convenience.
* **Never pool across dispersion types, units, timepoints or change-vs-raw
  scores** without an explicit, recorded conversion.
* **A trial that reported no usable dispersion is absent from the numerator and
  present in the denominator.** Silence is not a missing value to impute.

---

## Sources consulted

Written from a research pass in August 2026; `clinicaltrials.gov` and
`aact.ctti-clinicaltrials.org` were both unreachable from this environment, so
schema details attributed to them below are from secondary sources and must be
confirmed against a live pull.

* [AACT database documentation](https://aact.ctti-clinicaltrials.org/documentation/219)
  and [data dictionary](https://aact.ctti-clinicaltrials.org/data_dictionary) --
  results table structure
* [`Merck/bards-aactreveal`](https://github.com/Merck/bards-aactreveal) -- an
  existing extraction layer over the same AACT results tables
* [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/about-api) and
  [results data element definitions](https://clinicaltrials.gov/policy/results-definitions)
* [FDA Table of Surrogate Endpoints](https://www.fda.gov/drugs/development-resources/table-surrogate-endpoints-were-basis-drug-approval-or-licensure)
  and its [background](https://pmc.ncbi.nlm.nih.gov/articles/PMC8894669/)
* [FDA Clinical Outcome Assessment Compendium](https://www.fda.gov/drugs/development-resources/clinical-outcome-assessment-compendium)
* [COMET Initiative database](https://comet-initiative.org/Resources/Database)
* [Europe PMC RESTful Web Service](https://europepmc.org/RestfulWebService)
* [CTIS public portal](https://www.clinicaltrialsregister.eu/ctr-search/search)
  and [EMA's public-portal summary](https://www.ema.europa.eu/en/documents/other/clinical-trial-information-system-ctis-public-portal-summary_en.pdf)
* Gao, C., Pradeepkumar, J., Das, T. et al.,
  [*A large-scale database for clinical trial outcomes and features*](https://www.nature.com/articles/s44360-026-00081-6),
  Nature Health (2026); dataset at [CTOD](https://chufangao.github.io/CTOD/)
* Wan, X. et al.,
  [*Estimating the sample mean and standard deviation from the sample size, median, range and/or interquartile range*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4383202/)
  (2014) -- the IQR and range estimators in D6
* [*Standardized mean differences in meta-analysis: a tutorial*](https://pmc.ncbi.nlm.nih.gov/articles/PMC11795939/)
  -- on preferring baseline SD for standardisation (D8)
* [*The Standard Error/Standard Deviation Mix-Up*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11239727/)
  -- why D6 is the highest-risk item in Tier 1
* [*Reporting of statistically significant results at ClinicalTrials.gov*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5129217/)
  -- the p-value baseline referenced in D9
* [FDA results-reporting compliance notice, March 2026](https://www.fda.gov/news-events/press-announcements/fda-reminds-more-2200-sponsors-and-researchers-disclose-trial-results)
  -- the ~70% posting rate used above
* [*Obstacles to the reuse of study metadata in ClinicalTrials.gov*](https://www.nature.com/articles/s41597-020-00780-z)
  -- prior art on why outcome measures resist reuse
