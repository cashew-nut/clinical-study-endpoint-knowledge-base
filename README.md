# Clinical Trial Endpoints: Ontology + Graph Pipeline

A refreshable, queryable DuckDB warehouse (plus a CLI) that decomposes clinical
trial endpoints (`PFS`, `change from baseline in FEV1`, ...) into structured,
queryable dimensions: form, measurement, reference, timepoint, threshold,
direction, scale, therapeutic area. Source data is
[AACT](https://aact.ctti-clinicaltrials.org) (Aggregate Analysis of
ClinicalTrials.gov), not the live ClinicalTrials.gov site/API.

This is a data pipeline project, not an app: no UI, everything is queryable
via the CLI or directly against the DuckDB warehouse file.

Build order and design rationale live in the implementation plan; this README
covers setup, refresh, and ad hoc querying.

## Status

Build-order step 1 (scaffold + ingestion) is implemented: `endpoints pull`.
Everything else in `endpoints --help` is stubbed pending later steps (vocab
review, conforming pipeline, graph layer).

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

### AACT registration

1. Register for a free read-only account at https://aact.ctti-clinicaltrials.org
   (Postgres mirror of ClinicalTrials.gov, updated daily).
2. Copy `.env.example` to `.env` and fill in your credentials:

   ```
   PGHOST=aact-db.ctti-clinicaltrials.org
   PGPORT=5432
   PGDATABASE=aact
   PGUSER=<your username>
   PGPASSWORD=<your password>
   ```

   `.env` is gitignored. Credentials are read from the environment by DuckDB's
   `postgres` extension itself (libpq conventions via an empty `ATTACH ''`
   connection string) -- they are never interpolated into SQL or written to
   the warehouse.

## Usage

```bash
# Pull the 500 most recent Phase 3 studies (and their design_outcomes) into raw.*
uv run endpoints pull --phase 3 --limit 500

# Multiple phases, date filter
uv run endpoints pull --phase "2,3" --since 2023-01-01 --limit 500
```

Each `pull` is filtered and logged to `raw._pull_log` (pull_id, pulled_at,
filters_json, source_tables, row_counts). `raw.studies` / `raw.design_outcomes`
are replaced wholesale on each run, so re-running `pull` with the same (or
different) filters is a refresh, not a one-off script -- the pull history in
`raw._pull_log` accumulates across runs.

`--ta` (therapeutic area filter) is accepted by the CLI but not implemented
yet: TA is derived from a MeSH condition mapping built during the vocab
review step, not a native AACT field.

## Querying the warehouse directly

The warehouse (`warehouse.duckdb`, gitignored, created on first `pull`) is a
plain DuckDB file -- open it directly with the `duckdb` CLI for anything ad
hoc:

```bash
duckdb warehouse.duckdb

-- Studies pulled so far
SELECT phase, count(*) FROM raw.studies GROUP BY phase;

-- Pull history
SELECT pull_id, pulled_at, filters_json, row_counts FROM raw._pull_log ORDER BY pulled_at DESC;

-- Raw endpoint text for a given study
SELECT outcome_type, measure, time_frame, description
FROM raw.design_outcomes
WHERE nct_id = 'NCT00000000';
```

`endpoints query "<sql>"` and `endpoints export --query "<sql>" --format ...`
will be thin passthroughs to the same connection once implemented (build-order
step 4) -- the `duckdb` CLI above works today as the escape hatch.

## Repo layout

```
vocab/            controlled vocabularies (YAML), populated in build-order step 2
src/clinical_endpoints/
  db.py           DuckDB connection + AACT attach
  ingest/         AACT pull (filtered, logged)
  conform/        normalize / syntactic rules / semantic fallback / threshold+timepoint parsers (step 3)
  graph/          node/edge materialization (step 4)
  cli/            `endpoints` CLI
tests/
warehouse.duckdb   gitignored, created on first `pull`
```

## Development

```bash
uv run pytest
```
