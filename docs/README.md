# Documentation

## Guides

| document | what it is for |
|---|---|
| [`../README.md`](../README.md) | what this is, the features, and a quickstart from install to a USDM payload |
| [`USAGE.md`](USAGE.md) | every CLI command, its options, and what it writes |
| [`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md) | copy-pasteable SQL against `warehouse.duckdb` |
| [`../vocab/README.md`](../vocab/README.md) | the vocabulary schema, the judgment calls behind it, and measured coverage |

## Design specs

Each spec states its own status at the top. In short:

| spec | status | subject |
|---|---|---|
| [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md) | **implemented** | unbiased sampling of registry text for vocabulary review; MeSH → therapeutic-area resolution and the tree-vs-regex diff |
| [`USDM_ENDPOINTS_API_SPEC.md`](USDM_ENDPOINTS_API_SPEC.md) | **implemented** (phases 1–2; estimands and composites are not) | the design of record for the USDM 4.0 projection and its API |
| [`EVENT_SEMANTICS_SPEC.md`](EVENT_SEMANTICS_SPEC.md) | **implemented** (phases A–C; phase D unscheduled) | the event axis for time-to-event endpoints: what stops the clock, and why "PFS" is not "tumour burden" |
| [`USDM_PROJECTION_INTEGRITY_SPEC.md`](USDM_PROJECTION_INTEGRITY_SPEC.md) | **implemented** | announced defaults, per-attribute `derived` flags, timepoint roles, and the v2 extension profile |
| [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) | **implemented** | the results section: landing it, conforming it, normalising reported spread into a standard deviation, and `endpoints stats` |
| [`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md) | **implemented** (phases 1–4; phase 0, the measurement, needs a live pull) | grouping endpoints by the drug class under study: landing the interventions, the class vocabulary, the layered resolver, and `--drug-class` / `--by drug-class` |
| [`COMPOSITE_ENDPOINTS_SPEC.md`](COMPOSITE_ENDPOINTS_SPEC.md) | **not implemented** | decomposing composite endpoints into their components — a proposal, gated on a measurement that needs a live pull |
| [`RESULTS_CORRELATION_ROADMAP.md`](RESULTS_CORRELATION_ROADMAP.md) | **partly shipped** (D4–D9) | the menu the results spec was chosen from, and what was decided against |

Read them in that order if you are new to the project: the first two explain
how the pipeline gets its data and what it emits, the next two are corrections
made after validating the projection against a real trial, the fifth is what
the pipeline does with what trials reported, the sixth is how endpoints are
grouped by what was being tested, and the last two are proposals.

### Conventions

* A spec is named `<SUBJECT>_SPEC.md` and opens with a blockquoted **Status**
  line saying what is built, what is not, and where the code lives.
* Specs are amended in place rather than superseded by "v2" files; where one
  spec changes another, both say so in their status block.
* A "Standing constraints" section records the decisions a future change should
  not quietly undo.
* Specs are design records, not tutorials. Anything you would run lives in
  [`USAGE.md`](USAGE.md) or the [quickstart](../README.md#quickstart).

## What is not implemented

Beyond the composite spec above, three CLI commands are declared
and exit with a message: `endpoints query`, `endpoints export`, and `endpoints review
resolve`. See [`USAGE.md`](USAGE.md#not-implemented) for what to use instead.

Four measurements are owed by specs that are otherwise implemented, and all
four need an environment that can reach an ingestion backend: the MeSH
tree-prefix diff against real conditions
([`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md#still-owed)),
event coverage on event-family rows
([`EVENT_SEMANTICS_SPEC.md`](EVENT_SEMANTICS_SPEC.md#migration-and-measurement)),
and the four results-section numbers
([`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md#the-gate)), and the four
drug-class counts
([`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md#phasing-with-a-gate)) — the last two
of which have commands, `endpoints results coverage` and `endpoints drug-class
coverage`, waiting to take them.
