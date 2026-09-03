# Design spec: the results section, and endpoint variability

> **Status: implemented** (D4–D9 of
> [`RESULTS_CORRELATION_ROADMAP.md`](RESULTS_CORRELATION_ROADMAP.md)'s Tier 1).
> The code is `ingest/results.py` and `ingest/aact_results.py` (landing),
> `results/pipeline.py` (conforming and normalising), `results/dispersion.py`,
> `results/units.py` and `results/effects.py` (the arithmetic), and
> `results/stats.py` with `endpoints stats` (the output). D10 (power
> archaeology) and every Tier 2 item are not implemented.
>
> One thing the roadmap asked for was **not** done, and could not be: the gate
> pull. See [The gate](#the-gate).

Read alongside:

* [`RESULTS_CORRELATION_ROADMAP.md`](RESULTS_CORRELATION_ROADMAP.md) — where
  these deliverables come from, and what was decided against
* [`../vocab/README.md`](../vocab/README.md), "Coverage, honestly" — the
  measured limits of the join key all of this depends on
* [`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md), "Endpoint variability" — the
  SQL against what this writes

---

## What this adds, in one paragraph

`conformed.endpoints` made registry endpoints groupable. This attaches what the
trials in each group actually found. Five `raw.outcome_*` tables land the
results section both backends were already fetching; `conformed.endpoint_results`
conforms the reported titles through the *existing* engine and links them back
to the planned endpoints; `conformed.endpoint_dispersion` turns the several
things registries call "dispersion" into one estimated standard deviation and
records how; and `endpoints stats` reports the resulting distribution with the
denominator that makes it honest.

## The five raw tables

| table | grain | key |
|---|---|---|
| `raw.outcome_measures` | one reported outcome | `outcome_id` |
| `raw.outcome_groups` | one arm within one outcome | `(outcome_id, group_key)` |
| `raw.outcome_measurements` | one arm × class × category | `(outcome_id, group_key, class, category)` |
| `raw.outcome_analyses` | one statistical comparison | `analysis_id` |
| `raw.baseline_measurements` | one baseline characteristic × arm | `(baseline_id, group_key)` |

Three decisions carry from the existing ingest layer and were not relitigated:
both backends land the same shape, `ensure_table` reconciles the schema rather
than dropping and refetching, and a re-pull replaces a study's child rows
rather than appending to them.

**`outcome_id` is a content hash, not the source's own id.** The API's
`outcomeMeasures[]` entries carry no id at all and AACT's `outcomes.id` is a
surrogate that is not stable across AACT's own rebuilds, so the key is
`md5(nct_id | lower(outcome_type) | title | time_frame | duplicate_ordinal)` —
computed in Python by `ingest/results.py` and in SQL by the same module's
`outcome_id_sql`, with a test pinning the two spellings to each other. The
`outcome_type` is case-folded into the key (and stored verbatim on the row)
because AACT writes `Primary` where the API writes `PRIMARY`, and re-pulling a
study through the other backend must not renumber it.

**Every enumerated field is stored verbatim.** `param_type`,
`dispersion_type`, `unit_of_measure` and the analysis `param_type` land as the
registry wrote them. See [The gate](#the-gate) for why that is a design
decision rather than laziness.

### Landing is on by default

`pull` lands the results section unless given `--no-results`. The CT.gov API
returns `resultsSection` inside the study record the pull already fetches —
`_fetch_page` sends no `fields` parameter — so skipping it saves no network at
all, only warehouse size. `raw.studies.has_results` is landed either way,
because it is the denominator for "how much of this warehouse *could* have
results" and `--no-results` must not hide it.

On the AACT side, every column name is introspected rather than assumed
(`ingest/aact_results.py`), following the precedent `_pull_mesh_terms` already
set: a missing column becomes `NULL`, and a missing table costs the results
section rather than the pull.

## Linking results to the vocabulary

There is no second matcher. A results-section title is the same kind of free
text the conformance engine already handles, so it goes through `conform_row`
unchanged with the reported title standing in for `measure`.
`conformed.endpoint_results` therefore carries the same dimension columns, by
the same names, as `conformed.endpoints` — a test asserts it — so a query
written against the planned half works unchanged against the reported half.

The link back to the planned endpoint carries its own provenance:

| `link_method` | what it means |
|---|---|
| `exact_title` | the reported title is, verbatim after normalisation, a planned `measure` in the same study |
| `conformed_measurement` | different strings, same conformed `measurement_id` in the same study |
| `NULL` | no planned counterpart |

Three properties of this are deliberate.

**The link is many-to-one and title-only.** A sponsor that reports one planned
outcome at three timepoints produces three results rows all pointing at the
same planned endpoint. That is right: the timepoint is carried on the results
row itself, and it is the results row's timepoint that `stats` groups by.

**An unlinked reported outcome is a finding, not an error.** Sponsors report
outcomes they never registered. Force-joining one to the nearest planned
endpoint would be the most damaging thing this pipeline could do, so it stays
unlinked and goes to `conformed.results_review_queue` with reason
`unlinked_to_planned`.

**Baseline characteristics are never queued as unlinked.** They have no planned
counterpart by construction, and queueing them would bury the reported outcomes
that genuinely are.

`conformed.results_review_queue` is its own table rather than rows in
`conformed.review_queue` because `conform` wholesale-replaces the latter;
results rows kept there would be silently deleted by the next protocol-side
run.

## The dispersion normaliser

Reported spread is not a standard deviation. `results/dispersion.py` emits one
`sd_estimate` per arm-level measurement plus the path that produced it:

| reported as | conversion | flags |
|---|---|---|
| standard deviation | identity | — |
| standard error | `SE × √n` | derived |
| *k*% confidence interval | `(upper − lower) × √n / (2 z_k)` | derived |
| inter-quartile range | Wan et al. (2014) `IQR / η(n)` | derived, approximate |
| full range | Wan et al. (2014) `range / ξ(n)` | derived, approximate |
| geometric CV | `√(ln(1 + CV²))`, log scale | derived |

`z_k` comes from the confidence level stated in the string, never assumed to be
95 — a 90% interval is 18% narrower, and reading it as 95% understates the SD
by that much. `η(n)` and `ξ(n)` are computed per sample size rather than
collapsed to the textbook 1.35, which matters on small trials.

Five things it refuses:

* **A confidence interval reported around a median.** The width-to-SD formula
  inverts the standard error *of a mean*. Wan's estimators are the median's
  counterpart and they take an IQR or a range, never a CI.
* **A count-typed `param_type`.** `Count of Participants` rows carry a
  dispersion column too, and pooling their spread would put participant counts
  and litres in one distribution.
* **Anything without a trustworthy arm `n`**, where the conversion needs one.
  The `n` used and where it came from (`measurement`, `outcome_group`,
  `baseline_row`) are both recorded.
* **An unrecognised `dispersion_type`.** Reported, with its raw string, never
  coerced.
* **Converting a log-scale SD by a unit factor.** A factor applies to the
  quantity; taking logs turns multiplication into addition.

Every refusal writes an `sd_skip_reason`. A row that yielded no SD is in the
denominator and out of the numerator — never imputed, never dropped.

### Units

`unit_of_measure` is a field whose *entire content* is the unit, which is a
different register from protocol prose. `matching.yaml` sets
`min_synonym_length: 2`, so the generic matcher rightly never matches the
single-character synonym `L` inside a sentence; as the whole content of the
unit field, `L` is unambiguous. `results/units.py` therefore tries a
whole-field comparison first (honouring the same case-sensitivity rule for
short acronyms), then the parenthetical form `Liters (L)`, then the generic
in-prose matcher, then `scales.yaml`'s declared `default_when_unmatched`.

`scales.yaml` gained a fourth vocabulary round for this. `factor_to_si` went
from 21 of 59 terms to **58 of 67** — every family where the conversion is
exact unit algebra, plus self-anchoring for the singleton families. Nine terms
remain deliberately unconvertible, each with the reason on the term:
`percent_change` and `ratio` are different quantities rather than two spellings
of one; `percent_hba1c` is affine, not a factor; the mass-concentration and
molar-concentration families are not linked because `mg/dL ↔ mmol/L` needs the
analyte's molar mass, which is a property of the measurement and not of the
unit; the immunogenicity titres and `count_per_period` are left for a reviewer;
`dimensionless` and `not_stated` are catch-alls. `vocab validate` now also
checks that a unit family's anchor anchors itself at factor 1, so `to_si` can
never convert into a unit that is itself expressed in something else.

## `endpoints stats`

```
$ endpoints stats --measurement fev1

measurement=fev1, source=outcome

  change_from_baseline · litres  (converted via scales.yaml)
    studies 2      arms 4      participants 778
    SD      median 0.3   IQR 0.2793-0.31   range 0.247-0.31
            reported 3 · from_inter_quartile_range 1
    timepoints  single_fixed (2)
    coverage    2 of 2 conformed studies reported a usable dispersion (100.0%)

No SD from: dispersion_type_unrecognised 1
```

The shape of that output is the argument.

**One block per (form, unit) group, always.** The SD of FEV1 *change from
baseline* is not the SD of FEV1, and the SD in litres is not the SD in
millilitres. Rather than refuse to answer without three flags, `stats` groups
and reports each group separately; `--form` and `--scale` narrow, they do not
enable. The grouping unit is the converted one where `scales.yaml` declares a
conversion and the reported one where it does not — and because `si_scale_id`
is a function of `scale_id`, a group never mixes converted and unconverted
values.

**The coverage line is not decoration.** It is the share of *conformed studies
for that endpoint* that reported a usable dispersion. Excluding derived
estimates with `--only-reported` shrinks the numerator and leaves the
denominator alone, which is the whole point.

**The method mix is never collapsed.** A library built mostly out of
range-derived estimates is a different object from one built out of reported
SDs, and `--no-approximate` exists so the difference is actionable.

`--source baseline` (D8) selects baseline characteristics instead. It is a
parallel source, not a fallback: for a change-from-baseline endpoint the SD of
the change score and the SD of the raw baseline value are different quantities,
and converting between them needs the baseline/follow-up correlation, which
registries do not report. Nothing substitutes one for the other silently.

`--analyses` (D9) reports effect sizes, p-values and non-inferiority margins
instead of the SD distribution. Ratio-scale effects (hazard, odds, risk ratios)
pool on the effect alone because they are dimensionless whatever the endpoint
was measured in; difference-scale effects are converted to the group's unit
before pooling, because a mean difference of 120 mL and one of 0.23 L are the
same size and their unconverted median is a number about nothing. A censored
p-value (`<0.001`) contributes its bound and is counted separately from an
observed one. An NI margin is read out of the free-text description only where
the word "margin" introduces exactly one candidate number; where it does not,
the description is shown rather than a guessed number — a table of NI margins
per endpoint does not exist publicly in any form, so the one built here must
not be seeded with numbers that were never margins.

## The gate

The roadmap gated this whole tier on one live pull measuring four numbers
before any of it was designed in full:

1. what share of conformed studies have `hasResults`;
2. what share of results-section titles match the planned `measure` exactly,
   and what the conformance engine does with the rest;
3. the observed distribution of `dispersion_type` and `param_type`, and the
   exact value sets both use;
4. the share of results rows whose `unit_of_measure` normalises against
   `scales.yaml` as it stands.

**That pull was not run, and could not be.** `clinicaltrials.gov` and
`aact.ctti-clinicaltrials.org` are both unreachable from this project's build
environment — the same constraint `ingest/ctgov_api.py`'s CAVEAT and
`ta_mesh_mapping.yaml`'s caveats block already record — and an egress policy is
not something code can work around.

So the gate ships as an instrument rather than as a number. `endpoints results
coverage` reports all four against whatever has been landed, and everything
downstream was built to be measured rather than to assume a measurement:

* both enumerations are recognised from an **open** set of markers, so a
  spelling this project has never seen lands in the right kind where it can,
  and as `unknown` where it cannot;
* an unrecognised value is reported with its raw string, never coerced, so
  `results coverage` names exactly which strings the vocabulary is missing;
* every unresolved unit is listed the same way.

This is not a substitute for the measurement. It is how to take it:

```bash
uv run endpoints pull --phase 3 --limit 500
uv run endpoints conform
uv run endpoints results conform
uv run endpoints results coverage
```

### What was not measured

Everything below is stated from documentation and secondary sources, and should
be confirmed by the first real run rather than trusted:

* the exact value sets of `param_type` and `dispersion_type` on both sources;
* the field names inside `resultsSection` (`spread`, `lowerLimit`,
  `upperLimit`, `denoms[].counts[]`, `analyses[].nonInferiorityType`);
* AACT's results-table column names — which is why they are introspected;
* the real share of reported titles that match a planned `measure` exactly.

If a field name below is wrong, it surfaces as an empty column rather than as a
crash, and `results coverage` is where it shows up.

## The statistics, honestly

The roadmap's six caveats all survive into the implementation, and each has a
place in the output rather than only in a footnote:

| caveat | where it shows |
|---|---|
| selection into the results database is not random | `results coverage` section 1 |
| conformance bias compounds it | the coverage line's denominator |
| dispersion type is not standard deviation | `sd_method`, and the method mix on every group |
| units are free text | `scale_match_method`, and `results coverage` section 4 |
| timepoint and population are part of the endpoint | `timepoint_pattern` is a reported grouping key; **population is not**, and is stated as a limitation below |
| change-score SD and raw SD are different quantities | separate `form_id` groups, and D8's separate `--source` |

The one that has no home in the schema is **population**. `population` is
carried through as raw text on both the planned and the reported side and is
not a structured axis, so `stats` cannot narrow to a per-protocol or
enrichment population. An SD pooled across a severe-disease enrichment
population and a broad one is pooled across a real difference. That is a
limitation of the vocabulary, not of this tier, and it is stated rather than
papered over.

The honest reading of a `stats` block: *across N trials that reported a usable
dispersion for this endpoint in this unit, arm-level SD had median X and
interquartile range Y–Z; here is the coverage, and here is how each estimate
was derived.* Every clause is load-bearing.

## Standing constraints

Decisions a future change should not quietly undo.

* **Results ingestion stays a thin fetch-and-land**, source-agnostic across
  both backends. No conforming in the ingest layer.
* **Results text conforms through the existing engine.** No second matcher, no
  second vocabulary. If the two halves of the warehouse stop sharing one
  vocabulary they stop meaning the same thing.
* **Registry enumerations are landed verbatim and folded downstream.** The
  ingest layer never decides which values are allowed.
* **Every derived statistic records how it was derived.** `sd_method`,
  `sd_is_derived`, `sd_is_approximate` and the inputs are as non-negotiable as
  `match_method` and `confidence` are on the conformance side.
* **Every aggregate ships its denominator.** A `stats` output without a
  coverage line is a defect, not a terse convenience.
* **Never pool across dispersion types, units, timepoints, or change-vs-raw
  scores** without an explicit, recorded conversion. A `sd_estimate` and a
  `sd_estimate_si` are separate columns so that pooling is a decision.
* **A trial that reported no usable dispersion is absent from the numerator and
  present in the denominator.** Silence is not a missing value to impute.
* **An unlinked results row is never force-joined** to the nearest planned
  endpoint.
* **No pooled effect estimates.** Distributions, yes; a pooled treatment effect
  across trials grouped only by conformed endpoint is a systematic review, not
  a query.

## Sources

* Wan, X. et al., [*Estimating the sample mean and standard deviation from the
  sample size, median, range and/or interquartile
  range*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4383202/) (2014) — the
  IQR and range estimators
* [*The Standard Error/Standard Deviation
  Mix-Up*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11239727/) — why the SE
  path is the highest-risk conversion here
* [*Standardized mean differences in meta-analysis: a
  tutorial*](https://pmc.ncbi.nlm.nih.gov/articles/PMC11795939/) — on
  preferring baseline SD for standardisation (D8)
* [ClinicalTrials.gov results data element
  definitions](https://clinicaltrials.gov/policy/results-definitions) and the
  [API](https://clinicaltrials.gov/data-api/about-api)
* [AACT data dictionary](https://aact.ctti-clinicaltrials.org/data_dictionary)
  — the results table structure, introspected rather than trusted
* [*Reporting of statistically significant results at
  ClinicalTrials.gov*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5129217/) —
  the p-value baseline D9 is a finer instrument for
