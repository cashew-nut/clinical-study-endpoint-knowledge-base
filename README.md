# Clinical Study Endpoint Knowledge Base

Public study registrations describe their endpoints in free text -- `"PFS"`,
`"Change from baseline in FEV1 at Week 24"`, `"Proportion of participants
achieving PASI 75"`. Three sponsors write the same endpoint three ways, so
nothing about them is comparable across studies without first deciding what
each string actually says.

This project makes that decision explicit and re-runnable. It holds a library
of controlled vocabularies for the parameters an endpoint is built from, a
conformance engine that maps registry free text onto that library, an
in-process database to explore the result, an API that projects any single
study's endpoints into CDISC USDM 4.0 -- and, on top of that join key, an
empirical answer to *what variability should I expect for this endpoint?*,
assembled from what the trials themselves reported, sliceable by the drug class
under study.

Everything is a CLI plus a DuckDB file -- no server to stand up, no UI.

* [Features](#features) · [Install](#install) · [Quickstart](#quickstart)
* Usage examples: [`docs/USAGE.md`](docs/USAGE.md) (the CLI, end to end) and
  [`docs/QUERY_CHEATSHEET.md`](docs/QUERY_CHEATSHEET.md) (SQL against the
  warehouse); [`docs/QUESTIONS_THIS_ANSWERS.md`](docs/QUESTIONS_THIS_ANSWERS.md)
  is the same material framed as the questions it answers
* Reference: [`vocab/README.md`](vocab/README.md) (the vocabulary schema) and
  [`docs/README.md`](docs/README.md) (design specs, and what is implemented)

## Features

### 1. An endpoint library

[`vocab/`](vocab/) is a set of versioned YAML controlled vocabularies, one per
dimension, holding the reusable parameters that endpoints across studies are
assembled from:

| file | terms | what it names |
|---|---|---|
| `forms.yaml` | 18 | what kind of number the endpoint is (a change from baseline, a responder proportion, a time to event) |
| `measurements.yaml` | 242 | what quantity or event it is about (FEV1, PASI, vital status) |
| `references.yaml` | 17 | what it is measured against (own baseline, randomisation, comparator arm) |
| `events.yaml` | 38 | what occurrence stops the clock on a time-to-event endpoint |
| `named_endpoints.yaml` | 12 | what a literature name means (PFS, OS, MACE) |
| `directions.yaml` | 7 | which way is better -- derived from form + measurement, never matched |
| `scales.yaml` | 67 | the unit |
| `timepoint_patterns.yaml` | 11 | `time_frame` categories, and the regexes that extract values from them |
| `therapeutic_areas.yaml` | 24 | therapeutic area, resolved from MeSH |
| `drug_classes.yaml` | 116 | what the study was testing -- mechanism, pharmacologic class, modality, or control |
| `usdm_templates.yaml` | 18 | one USDM syntax template per form |

Three files are not term lists. `matching.yaml` states *how* a synonym is
compared to a registry string, `ta_mesh_mapping.yaml` maps MeSH conditions and
interventions to therapeutic areas, and `drug_class_mesh_mapping.yaml` maps
registered interventions to drug classes.

The library is the contract: `endpoints vocab validate` checks id uniqueness,
synonyms claimed by two terms, regex compilability, cross-file referential
integrity and closed value sets, then loads all of it into `vocab.*` tables.
The conforming pipeline reads those tables, never the YAML, so a run is always
against a validated snapshot -- and an edit under `vocab/` simply does not take
effect until you re-validate.

See [`vocab/README.md`](vocab/README.md) for the schema, the judgment calls
behind the category boundaries, and measured coverage.

### 2. A conformance pipeline

`endpoints conform` maps each free-text registry outcome onto the library, in
two layers:

* **Syntactic.** `matching.yaml`'s cascade -- `measure` → `description` →
  `time_frame`, `exact` → `syntactic_rule` -- resolves form, measurement,
  reference and event; the timepoint classifier runs preprocessing, guard
  patterns and prioritised regexes to extract a structured timepoint; the
  threshold parser pulls comparator, value and unit out of the string.
* **Semantic.** Where the cascade leaves measurement unresolved, a
  token-overlap fallback proposes the closest term. Measurement is the one
  dimension allowed no default: a row whose measurement resolves nowhere goes
  to `conformed.review_queue` rather than being conformed at low confidence.

Direction is *derived*, never matched, from the resolved form's
`direction_rule` and the measurement's polarity -- so "overall survival" and
"mortality rate" get opposite directions without either string saying so.

Every dimension records how it was decided (`exact`, `syntactic_rule`,
`semantic`), from which source field, and at what confidence. A wrong answer is
therefore auditable, and a low-confidence one is filterable.

### 3. An in-process database

The warehouse is one DuckDB file, `warehouse.duckdb` -- no server, no client to
install. `conformed.endpoints` is a flat star-schema row per endpoint with typed
foreign keys into the vocabulary, so exploration is plain SQL:

```sql
-- which measurements were expressed as more than one kind of number?
SELECT measurement_id, count(DISTINCT form_id) AS forms, count(*) AS endpoints
FROM conformed.endpoints
GROUP BY 1 HAVING count(DISTINCT form_id) > 1
ORDER BY 2 DESC, 3 DESC;
```

[`docs/QUERY_CHEATSHEET.md`](docs/QUERY_CHEATSHEET.md) is a page of
copy-pasteable queries: conformance coverage and where the loss is, cross-study
comparability, threshold and timepoint distributions, and the vocabulary tables
themselves.

### 4. An endpoint statistics reference

Once endpoints can be grouped, the results the trials reported can be attached
to the group. `pull` lands the results section both ingestion backends were
already fetching and discarding; `endpoints results conform` runs the *same*
conformance engine over the reported outcome titles and normalises what each
trial called "dispersion" -- standard deviations, standard errors, confidence
intervals, inter-quartile ranges -- into one estimated standard deviation, with
the conversion recorded on every row.

```
$ endpoints stats --measurement fev1

measurement=fev1, source=outcome

  change_from_baseline · litres  (converted via scales.yaml)
    studies 2      arms 4      participants 778
    SD      median 0.3   IQR 0.2793-0.31   range 0.247-0.31
            reported 3 · from_inter_quartile_range 1
    timepoints  single_fixed (2)
    coverage    2 of 2 conformed studies reported a usable dispersion (100.0%)
```

Nobody publishes an empirical prior for the variability of a given endpoint at
a given timepoint; every statistician assembling a sample-size calculation
reconstructs it by hand from two or three papers they happen to know.

The output is grouped by form and unit because the SD of a change from baseline
is not the SD of a raw value and the SD in litres is not the SD in millilitres,
and the coverage line is not decoration -- without it the command is a machine
for producing confident numbers off eight arms. `--source baseline` gives the
baseline SD as its own quantity rather than as a fallback, and `--analyses`
gives the effect-size, p-value and non-inferiority-margin distributions
instead.

See [`docs/ENDPOINT_RESULTS_SPEC.md`](docs/ENDPOINT_RESULTS_SPEC.md) for what
is converted, what is refused, and the four measurements this still owes a live
pull.

### 5. A drug-class axis

`conformed.endpoints` groups endpoints by *what was measured*. This groups them
by *what was being tested*. `pull` lands the interventions both backends were
already returning and discarding, and resolves them through a second layered
vocabulary into `conformed.study_drug_class`:

```
$ endpoints stats --measurement hba1c --by drug-class

── glp1_receptor_agonist ──
measurement=hba1c, drug_class=glp1_receptor_agonist, source=outcome
  change_from_baseline · percent
    studies 6      arms 14     participants 4,201
    ...
── sglt2_inhibitor ──
...
```

Every class declares a `kind` -- `mechanism` (GLP-1 receptor agonist),
`pharmacologic` (antineoplastic agent), `modality` (monoclonal antibody) or
`control` (placebo) -- and the split is mandatory, because a `GROUP BY` that
mixes them compares "PD-1 inhibitor" against "monoclonal antibody" as though
they were alternatives. Classification is layered the same way therapeutic area
is: hand-settled overrides, a curated agent dictionary, **WHO INN stems** (which
is what classes a drug approved after the vocabulary was written), NLM's own
MeSH ancestry, and the registry's coarse browse branches -- with an intervention
that matches nothing going to `conformed.drug_class_review_queue` rather than
being guessed at.

`--drug-class` filters and `--by drug-class` stratifies, and the difference
matters: on the SD side the stratifier is a homogeneity check, but on
`--analyses` it is the point, because a median treatment effect pooled across
mechanisms has no referent.

Chemical structure is deliberately not modelled, and ATC codes are not
available: ClinicalTrials.gov carries none, on either backend. See
[`docs/DRUG_CLASS_SPEC.md`](docs/DRUG_CLASS_SPEC.md) for what each layer claims
and the four counts a first live pull still owes the axis.

### 6. A USDM 4.0 projection API

`endpoints usdm show <NCT_ID>` projects one study's endpoints into CDISC USDM
4.0, and `endpoints serve` exposes the same projection over HTTP at
`GET /v4/studies/{nctId}/endpoints`.

Each endpoint's `text` is a *syntax template* --
`<p>Change from <usdm:tag name="reference"/> in <usdm:tag name="measurement"/>
…</p>` -- whose tags resolve, through that endpoint's own
`SyntaxTemplateDictionary`, back into the controlled vocabularies. The registry
string is kept verbatim in `description`, so the projection is auditable rather
than a rewrite, and every synthesized or defaulted attribute is flagged as such.

Every raw outcome row becomes exactly one USDM `Endpoint`, at one of three
fidelity tiers -- `templated`, `partial`, or `verbatim` -- so a study whose
endpoints did not conform renders less richly, never appears to have fewer
endpoints.

### Ingestion

Study registrations -- and, for studies that posted them, results -- come from
ClinicalTrials.gov, via either of two interchangeable backends that land the
same `raw.*` shape: the public
[CT.gov API v2](https://clinicaltrials.gov/data-api/api) (default, no auth) or
[AACT](https://aact.ctti-clinicaltrials.org) (`--source aact`, needs free
credentials). It is a thin fetch-and-upsert, deliberately -- everything
downstream is source-agnostic. `pull` filters by phase, date, therapeutic area
(`--ta`), drug class (`--drug-class`) and lead-sponsor organisation (`--org`),
all applied before `--limit`; `--replace` opts out of the upsert to replace raw.* with
just that one pull instead, and `--no-results` skips the results section (which
saves warehouse size, never network -- the API returns it in the payload the
pull already fetches). See
[`docs/USAGE.md`](docs/USAGE.md#ingesting-studies) for the backends and their
trade-offs.

## Install

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                    # the CLI and pipeline
uv sync --extra serve      # ...plus FastAPI/uvicorn, if you want `endpoints serve`
```

## Quickstart

The whole flow, from an empty directory to a USDM payload for one study.

**1. Validate the vocabularies and load them into the warehouse.** Everything
downstream reads `vocab.*` tables, not the YAML, so this is what a run is
always against.

```bash
uv run endpoints vocab validate
```

```
Vocabulary valid (direction=7, drug_class=116, event=38, form=18,
measurement=242, reference=17, scale=67, therapeutic_area=24,
timepoint_pattern=11)
Wrote 5298 rows across 52 vocab.* tables -> warehouse.duckdb
```

You can skip straight to step 2 if you like: a `pull` into a warehouse that
holds no vocabulary loads one itself, so a first run is never split into a
landing wave and a classifying wave. Run this yourself when you want to see the
validation output, or after editing anything under `vocab/` -- an edit takes
effect only on re-validation, and `pull` never rewrites a vocabulary the
warehouse already holds.

**2. Ingest studies.** Filtered by phase, date, therapeutic area, drug class or
lead sponsor. This creates `warehouse.duckdb` if it does not exist and writes
`raw.studies`, `raw.design_outcomes`, the condition and intervention tables,
and the results section -- then resolves the therapeutic-area and drug-class
axes over what it landed. One pull, one wave; there is no separate pull per
axis.

```bash
uv run endpoints pull --phase 3 --limit 500                 # 500 most recent Phase 3 studies
uv run endpoints pull --phase 3 --limit 500 --ta oncology    # ...only oncology
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer"  # ...only Pfizer-led studies
uv run endpoints pull --phase 3 --limit 500 --drug-class sglt2_inhibitor   # ...only SGLT2 trials
```

Re-running `pull` upserts: studies matched by *this* pull are refreshed in
place, studies landed by earlier pulls with other filters are left alone. Add
`--replace` to opt out of that and land only this pull's studies instead --
see [`docs/USAGE.md`](docs/USAGE.md#what-a-pull-does-to-what-is-already-there).

**3. Conform the endpoints.** Reads `raw.design_outcomes` and `vocab.*`, writes
`conformed.endpoints` and `conformed.review_queue`.

```bash
uv run endpoints conform
```

```
Conformed 3812 of 4196 row(s) -> conformed.endpoints; 384 -> conformed.review_queue
```

(Row counts depend on what you pulled.)

**4. Look at what did not conform** -- the review queue is the honest part of
the coverage number, and the input to the next vocabulary round.

```bash
uv run endpoints review list --reason measurement_unmatched
```

**5. See what the corpus was testing.** `pull` already resolved it; this is how
you read it.

```bash
uv run endpoints drug-class distribution --kind mechanism
uv run endpoints drug-class coverage     # ...and how much of the corpus is behind that
```

**6. Conform what the trials reported**, and ask what variability to expect.

```bash
uv run endpoints results conform
uv run endpoints stats --measurement fev1
uv run endpoints stats --measurement fev1 --by drug-class   # ...split by mechanism
uv run endpoints results coverage    # how much of the corpus is behind that answer
```

**7. Explore the conformed endpoints** with any DuckDB client, or the project's
own connection:

```bash
duckdb warehouse.duckdb -c "
  SELECT form_id, count(*) FROM conformed.endpoints GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

More: [`docs/QUERY_CHEATSHEET.md`](docs/QUERY_CHEATSHEET.md).

**8. Project one study into USDM 4.0.**

```bash
uv run endpoints usdm show NCT04162249                        # the endpoints module
uv run endpoints usdm show NCT04162249 --level primary        # primary endpoints only
uv run endpoints usdm show NCT04162249 --envelope wrapper -o study.json
uv run endpoints usdm coverage                                # tier mix across the corpus
```

**9. Or serve it** (needs `uv sync --extra serve`):

```bash
uv run endpoints serve --port 8000
curl localhost:8000/v4/studies/NCT04162249/endpoints
```

Every command that touches the warehouse takes `--warehouse <path>`, so several
can sit side by side -- one per therapeutic area, one per vocabulary revision.

## Usage examples

* [`docs/USAGE.md`](docs/USAGE.md) -- every command, its options and what it
  writes: ingestion backends and AACT setup, vocabulary validation and
  sampling, conforming and the review queue, therapeutic-area resolution, the
  USDM projection and the HTTP API.
* [`docs/QUERY_CHEATSHEET.md`](docs/QUERY_CHEATSHEET.md) -- SQL for the
  warehouse: coverage, cross-study comparability, one study end to end.
* [`docs/CONFORMED_ERD.md`](docs/CONFORMED_ERD.md) -- the entity-relationship
  diagram of the `conformed` schema: every table's grain and key, what joins to
  what, and the five edges that are not what they look like.
* [`docs/QUESTIONS_THIS_ANSWERS.md`](docs/QUESTIONS_THIS_ANSWERS.md) -- the
  other direction: six clinical / study-design questions, and what running the
  pipeline gives back for each.
* [`vocab/README.md`](vocab/README.md) -- how to read and extend the
  vocabularies.
* [`docs/README.md`](docs/README.md) -- the design specs, each flagged
  implemented or not.

## Repo layout

```
vocab/                     the endpoint library (YAML) -- see vocab/README.md
src/clinical_endpoints/
  db.py                    DuckDB connection + AACT attach
  ingest/                  CT.gov API and AACT backends, shared filters, pull log, upsert
  vocab/                   vocabulary schema, validation/loading, review sampling
  conform/                 the conformance pipeline: normalisation, syntactic rules,
                           semantic fallback, timepoint and threshold parsers, direction
  results/                 the results section: linking it to the vocabulary, normalising
                           reported spread into an SD, and the `stats` distributions
  ta/                      MeSH condition/intervention -> therapeutic area
  drug_class/              registered interventions -> drug class, and the ancestor diff
  usdm/                    CDISC USDM 4.0 projection: templates, tags, ids, envelopes, API
  cli/                     the `endpoints` CLI
docs/                      usage, query cheat sheet, design specs
tests/
warehouse.duckdb           gitignored, created on first `pull` or `vocab validate`
```

## Development

```bash
uv run pytest
```

The suite runs without network access -- the ingestion backends are covered
against recorded payloads and everything downstream against fixture warehouses.
One AACT attach test skips itself when DuckDB's `postgres` extension cannot be
downloaded.
