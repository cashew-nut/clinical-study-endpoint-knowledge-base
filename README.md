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

Build-order steps 1 and 2 are implemented, including the two step-2 gaps
`docs/NEXT_SESSION.md` tracked (an unbiased/coverage-reported vocab sample, and
`pull --ta` backed by a real therapeutic-area resolver).

**Step 1 (scaffold + ingestion):** `endpoints pull`, including conditions and
MeSH-coded browse tables (`raw.conditions`, `raw.browse_conditions`,
`raw.browse_interventions`, plus a backend-specific `raw.mesh_terms` or
`raw.browse_condition_branches`).

**Step 2 (vocabulary):** `endpoints vocab sample` exports the distinct
measure/description/time_frame strings for human review -- either a per-field
frequency table (every value at or above `--min-frequency` kept uncapped, plus
a seeded random sample of the tail, with a coverage sidecar) or a joinable
row-level sample (`--format rows`) -- and the eight controlled vocabularies in
[`vocab/`](vocab/) are written and validated. `endpoints vocab validate` loads
them into `vocab.*` tables, checking id uniqueness, orphan/ambiguous synonyms,
regex compilability, and cross-file referential integrity. `endpoints pull --ta`
resolves each pulled study's therapeutic area(s) from its MeSH conditions/
interventions (`src/clinical_endpoints/ta/resolver.py`) into
`conformed.study_therapeutic_area`, keeping every matched area and filtering the
pull down to the requested one(s). `endpoints ta diff-tree` diffs the
tree-prefix layer against the regex layer per condition, to find wrong tree
prefixes or wrong regexes in `vocab/ta_mesh_mapping.yaml`. See
[`vocab/README.md`](vocab/README.md) for the schema, the judgment calls behind
the category boundaries, and measured coverage.

`conform`, `review`, `graph build`, `query`, and `export` are stubbed pending
steps 3 and 4 -- deliberately not started yet: conforming against a vocabulary
built from a biased sample would bake the bias in, so a fresh vocabulary review
(using the now-unbiased sampler) comes first. See `docs/NEXT_SESSION.md`.

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
# Validate the controlled vocabularies and load them into vocab.*
uv run endpoints vocab validate

# Pull the 500 most recent Phase 3 studies (and their design_outcomes) into raw.*
# (uses --source ctgov_api by default)
uv run endpoints pull --phase 3 --limit 500

# Multiple phases, date filter
uv run endpoints pull --phase "2,3" --since 2023-01-01 --limit 500

# Explicitly use AACT instead (needs .env credentials, see above)
uv run endpoints pull --phase 3 --limit 500 --source aact
```

Each `pull` is filtered and logged to `raw._pull_log` (pull_id, pulled_at,
source, filters_json, source_tables, row_counts) -- `source_tables` differs by
backend (AACT lands `mesh_terms`, the CT.gov API backend lands
`browse_condition_branches` instead; see "Ingestion backends" below). Every
`raw.*` table is replaced wholesale on each run, so re-running `pull` with the
same (or different) filters, or a different `--source`, is a refresh, not a
one-off script -- the pull history in `raw._pull_log` accumulates across runs.

```bash
# Once vocab validate has loaded the TA mapping, pull can filter by it
uv run endpoints vocab validate
uv run endpoints pull --phase 3 --limit 500 --ta oncology

# Multiple areas
uv run endpoints pull --phase 3 --limit 500 --ta oncology,cardiovascular
```

`--ta` (comma-separated therapeutic-area ids from `therapeutic_areas.yaml`)
requires `endpoints vocab validate` to have already loaded the MeSH -> TA
mapping into the warehouse. `pull` always resolves therapeutic areas for every
pulled study into `conformed.study_therapeutic_area` (all matched areas kept,
one marked `is_primary`) once the vocab is loaded, whether or not `--ta` is
given; `--ta` additionally filters `raw.*` down to studies matching one of the
requested areas. See [`vocab/README.md`](vocab/README.md) for the layered
MeSH -> TA mapping this resolves against, and its "Known gaps" for what's still
unverified against a live pull (this sandbox cannot reach either backend).

`endpoints ta diff-tree` runs the MeSH tree-prefix layer alone and the regex
layer alone over every pulled study's conditions and reports every
disagreement, most frequent first -- each one is either a wrong tree prefix or
a wrong regex in `ta_mesh_mapping.yaml`, and this diff is the only way to find
them without a MeSH expert.

### A note on the `ctgov_api` backend

This was built from ClinicalTrials.gov API v2's documented shape but has not
been verified against a live response -- the environment it was built in
can't reach `clinicaltrials.gov` either. If a `pull --source ctgov_api` call
fails with a `ClinicalTrials.gov API returned HTTP ...` error, the error
includes the full response body, which is normally enough to identify and
fix the one wrong field/param name (see `src/clinical_endpoints/ingest/ctgov_api.py`).

## Vocabulary

Versioned YAML files in [`vocab/`](vocab/), one per dimension, revised in
vocabulary review round two against an unbiased sample of 13,542 outcome rows:

| file | terms | dimension |
|---|---|---|
| `forms.yaml` | 18 | what kind of number the endpoint is |
| `measurements.yaml` | 165 | what quantity or event it is about |
| `references.yaml` | 16 | what it is measured against |
| `directions.yaml` | 7 | which way is better (derived, not matched) |
| `scales.yaml` | 59 | the unit |
| `therapeutic_areas.yaml` | 24 | therapeutic area |
| `timepoint_patterns.yaml` | 11 | `time_frame` categories + extraction regexes |
| `ta_mesh_mapping.yaml` | -- | MeSH condition/intervention -> TA |
| `matching.yaml` | -- | how all of the above are matched |

```bash
uv run endpoints vocab validate               # check, then write vocab.* tables
uv run endpoints vocab validate --check-only  # check without writing
uv run endpoints vocab validate --strict      # warnings become errors
```

Validation covers id uniqueness and format, synonyms claimed by more than one
term, regex compilability, cross-file referential integrity (a `default_scale`
that names no scale, a `direction_by_ta` keyed on a therapeutic area that does
not exist), match-precedence lists that have drifted out of step with their
terms, tied precedence/priority values, and closed value sets. Errors fail the
command and write nothing; warnings are reported and do not.

`vocab/README.md` documents the schema, the decisions worth reviewing, and
measured coverage. Round-two headline figures, population-weighted over the
corpus rather than over the sample: **form 76.0%**, **measurement 64.5%** (both
reading `description` where `measure` is silent), **timepoint 90.8%**. Those are
lower than round one's and answer a harder question -- round one measured
against the 500 most frequent strings per field, which is almost pure head.

`matching.yaml` is the one file that is not a term list. It states how a synonym
is compared to a registry string, and it exists because leaving that implicit
was not free: matching synonyms as substrings rather than whole tokens put 9.8%
of all outcome rows under one wrong measurement (`ess`, inside "assessment")
while making coverage look 23 points better than it was.

### Sampling `design_outcomes` for vocabulary review

```bash
# Per-field frequency table + coverage sidecar (default)
uv run endpoints vocab sample
uv run endpoints vocab sample --min-frequency 3 --singleton-sample 500 --seed 7

# Joinable row-level sample -- what measure had this time_frame/description?
uv run endpoints vocab sample --format rows --limit 1000

# Just primary outcomes, where efficacy endpoints concentrate
uv run endpoints vocab sample --outcome-type primary
```

Every value occurring `--min-frequency` (default 2) times or more is kept
uncapped; the tail below that is a seeded random sample (`--singleton-sample`,
default 300), not an alphabetical head, so the long tail of one-off endpoint
wordings is fairly represented rather than silently truncated at "starts with
A". `vocab_review_coverage.csv` (or `<out>_coverage.csv`) reports, per field,
what fraction of rows the kept values actually account for -- machine-readable,
so the next vocabulary round can diff it against this one.

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

-- Vocabulary: forms in match order, with how each derives Direction
SELECT p.rank, f.id, f.direction_rule
FROM vocab.term_precedence p JOIN vocab.forms f ON f.id = p.term_id
WHERE p.dimension = 'form' ORDER BY p.rank;

-- Measurement concepts covered by more than one instrument
SELECT concept, count(*) AS instruments, string_agg(id, ', ') AS ids
FROM vocab.measurements GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC;

-- Every synonym that resolves to a given measurement
SELECT synonym FROM vocab.synonyms
WHERE dimension = 'measurement' AND term_id = 'hba1c';

-- Therapeutic areas resolved for the studies pulled so far
SELECT ta_id, count(*) FROM conformed.study_therapeutic_area
WHERE is_primary GROUP BY 1 ORDER BY 2 DESC;
```

`endpoints query "<sql>"` and `endpoints export --query "<sql>" --format ...`
will be thin passthroughs to the same connection once implemented (build-order
step 4) -- the `duckdb` CLI above works today as the escape hatch.

## Repo layout

```
vocab/            controlled vocabularies (YAML) -- see vocab/README.md
src/clinical_endpoints/
  db.py           DuckDB connection + AACT attach
  ingest/
    filters.py    shared PullFilters / phase normalization
    pull_log.py   shared raw._pull_log writer
    aact.py       AACT backend
    ctgov_api.py  ClinicalTrials.gov API v2 backend (default)
  vocab/
    sample.py     `vocab sample` CSV export (step 2)
    schema.py     declarative description of the vocab/*.yaml files
    loader.py     `vocab validate`: parse, validate, write vocab.* tables
  ta/
    resolver.py   MeSH condition/intervention -> therapeutic area (pull --ta, ta diff-tree)
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
