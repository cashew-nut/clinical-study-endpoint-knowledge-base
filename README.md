# Clinical Study Endpoint Knowledge Base

Public study registrations describe their endpoints in free text -- `"PFS"`,
`"Change from baseline in FEV1 at Week 24"`, `"Proportion of participants
achieving PASI 75"`. Three sponsors write the same endpoint three ways, so
nothing about them is comparable across studies without first deciding what
each string actually says.

This project makes that decision explicit and re-runnable. It holds a library
of controlled vocabularies for the parameters an endpoint is built from, a
conformance engine that maps registry free text onto that library, an
in-process database to explore the result, and an API that projects any single
study's endpoints into CDISC USDM 4.0.

Everything is a CLI plus a DuckDB file -- no server to stand up, no UI.

* [Features](#features) · [Install](#install) · [Quickstart](#quickstart)
* Usage examples: [`docs/USAGE.md`](docs/USAGE.md) (the CLI, end to end) and
  [`docs/QUERY_CHEATSHEET.md`](docs/QUERY_CHEATSHEET.md) (SQL against the
  warehouse)
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
| `measurements.yaml` | 234 | what quantity or event it is about (FEV1, PASI, vital status) |
| `references.yaml` | 17 | what it is measured against (own baseline, randomisation, comparator arm) |
| `events.yaml` | 38 | what occurrence stops the clock on a time-to-event endpoint |
| `named_endpoints.yaml` | 12 | what a literature name means (PFS, OS, MACE) |
| `directions.yaml` | 7 | which way is better -- derived from form + measurement, never matched |
| `scales.yaml` | 67 | the unit |
| `timepoint_patterns.yaml` | 11 | `time_frame` categories, and the regexes that extract values from them |
| `therapeutic_areas.yaml` | 24 | therapeutic area, resolved from MeSH |
| `usdm_templates.yaml` | 18 | one USDM syntax template per form |

Two files are not term lists. `matching.yaml` states *how* a synonym is
compared to a registry string, and `ta_mesh_mapping.yaml` maps MeSH conditions
and interventions to therapeutic areas.

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

### 4. A USDM 4.0 projection API

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

Study registrations come from ClinicalTrials.gov, via either of two
interchangeable backends that land the same `raw.*` shape: the public
[CT.gov API v2](https://clinicaltrials.gov/data-api/api) (default, no auth) or
[AACT](https://aact.ctti-clinicaltrials.org) (`--source aact`, needs free
credentials). It is a thin fetch-and-upsert, deliberately -- everything
downstream is source-agnostic. See
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

**1. Validate the vocabularies and load them into the warehouse.** Do this
first: everything downstream reads `vocab.*` tables, not the YAML.

```bash
uv run endpoints vocab validate
```

```
Vocabulary valid (direction=7, event=38, form=18, measurement=234,
reference=17, scale=67, therapeutic_area=24, timepoint_pattern=11)
Wrote 3940 rows across 44 vocab.* tables -> warehouse.duckdb
```

**2. Ingest studies.** Filtered by phase, date, and -- because step 1 loaded the
MeSH → therapeutic-area mapping -- therapeutic area. This creates
`warehouse.duckdb` if it does not exist and writes `raw.studies`,
`raw.design_outcomes` and the condition/intervention tables.

```bash
uv run endpoints pull --phase 3 --limit 500                 # 500 most recent Phase 3 studies
uv run endpoints pull --phase 3 --limit 500 --ta oncology   # ...only oncology
```

Re-running `pull` upserts: studies matched by *this* pull are refreshed in
place, studies landed by earlier pulls with other filters are left alone.

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

**5. Explore the conformed endpoints** with any DuckDB client, or the project's
own connection:

```bash
duckdb warehouse.duckdb -c "
  SELECT form_id, count(*) FROM conformed.endpoints GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

More: [`docs/QUERY_CHEATSHEET.md`](docs/QUERY_CHEATSHEET.md).

**6. Project one study into USDM 4.0.**

```bash
uv run endpoints usdm show NCT04162249                        # the endpoints module
uv run endpoints usdm show NCT04162249 --level primary        # primary endpoints only
uv run endpoints usdm show NCT04162249 --envelope wrapper -o study.json
uv run endpoints usdm coverage                                # tier mix across the corpus
```

**7. Or serve it** (needs `uv sync --extra serve`):

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
  ta/                      MeSH condition/intervention -> therapeutic area
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
