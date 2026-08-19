# Clinical Trial Endpoints: Ontology + Graph Pipeline

A refreshable, queryable DuckDB warehouse (plus a CLI) that decomposes clinical
trial endpoints (`PFS`, `change from baseline in FEV1`, ...) into structured,
queryable dimensions: form, measurement, reference, timepoint, threshold,
direction, scale, therapeutic area. Source data is study registrations from
ClinicalTrials.gov, pulled via one of two interchangeable ingestion backends
(see [Ingestion backends](#ingestion-backends) below) into the *same*
`raw.studies` / `raw.design_outcomes` shape -- everything downstream is
source-agnostic.

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

## Ingestion backends

Two interchangeable backends, selected with `--source`, both writing the same
`raw.studies` / `raw.design_outcomes` shape:

| `--source` | Default? | Needs | Notes |
|---|---|---|---|
| `ctgov_api` | yes | nothing (no auth) | Public [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api). Current default because AACT access has been unreliable/unreachable (connection timeouts) for us. Per-outcome `population` is always NULL -- the API doesn't expose it. |
| `aact` | opt-in | `.env` credentials | [AACT](https://aact.ctti-clinicaltrials.org), a relational Postgres mirror of ClinicalTrials.gov. Richer/faster once reachable; was the original plan's default source specifically to avoid live-API rate limiting/blocking (see the implementation plan §2) -- flip back to it with `--source aact` once AACT access is resolved. |

### AACT registration (only needed for `--source aact`)

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
# (uses --source ctgov_api by default)
uv run endpoints pull --phase 3 --limit 500

# Multiple phases, date filter
uv run endpoints pull --phase "2,3" --since 2023-01-01 --limit 500

# Explicitly use AACT instead (needs .env credentials, see above)
uv run endpoints pull --phase 3 --limit 500 --source aact
```

Each `pull` is filtered and logged to `raw._pull_log` (pull_id, pulled_at,
source, filters_json, source_tables, row_counts). `raw.studies` /
`raw.design_outcomes` are replaced wholesale on each run, so re-running `pull`
with the same (or different) filters, or a different `--source`, is a
refresh, not a one-off script -- the pull history in `raw._pull_log`
accumulates across runs.

`--ta` (therapeutic area filter) is accepted by the CLI but not implemented
yet: TA is derived from a MeSH condition mapping built during the vocab
review step, not a native field on either source.

### A note on the `ctgov_api` backend

This was built from ClinicalTrials.gov API v2's documented shape but has not
been verified against a live response -- the environment it was built in
can't reach `clinicaltrials.gov` either. If a `pull --source ctgov_api` call
fails with a `ClinicalTrials.gov API returned HTTP ...` error, the error
includes the full response body, which is normally enough to identify and
fix the one wrong field/param name (see `src/clinical_endpoints/ingest/ctgov_api.py`).

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
  ingest/
    filters.py    shared PullFilters / phase normalization
    pull_log.py   shared raw._pull_log writer
    aact.py       AACT backend
    ctgov_api.py  ClinicalTrials.gov API v2 backend (default)
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
