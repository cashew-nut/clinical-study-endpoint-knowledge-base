# Usage

Every command, with the options worth knowing and what each one writes. For a
first run, start with the [quickstart](../README.md#quickstart); for SQL against
the result, see [`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md).

* [The warehouse](#the-warehouse)
* [Ingesting studies](#ingesting-studies)
* [Therapeutic areas](#therapeutic-areas)
* [The vocabulary](#the-vocabulary)
* [Conforming endpoints](#conforming-endpoints)
* [The results section](#the-results-section)
* [Endpoint variability](#endpoint-variability)
* [Projecting to USDM 4.0](#projecting-to-usdm-40)
* [Serving the API](#serving-the-api)
* [Not implemented](#not-implemented)

## The warehouse

One gitignored DuckDB file, `warehouse.duckdb`, created by whichever command
runs first. Three schemas:

| schema | written by | holds |
|---|---|---|
| `raw` | `pull` | studies, design outcomes, conditions, MeSH browse rows, the registered interventions (`interventions`, `arm_interventions`, `browse_intervention_*`), the results section (`outcome_*`, `baseline_measurements`), the pull log |
| `vocab` | `vocab validate` | the endpoint library, loaded from `vocab/*.yaml` |
| `conformed` | `conform`, `results conform`, `pull` | `endpoints`, `review_queue`, `study_therapeutic_area`, `study_drug_class`, `arm_drug_class`, `drug_class_review_queue`, `endpoint_results`, `endpoint_dispersion`, `results_review_queue` |

Every command that touches the warehouse takes `--warehouse <path>`, so several
can sit side by side (one per therapeutic area, one per vocabulary revision):

```bash
uv run endpoints vocab validate --warehouse onc.duckdb
uv run endpoints pull --phase 3 --ta oncology --warehouse onc.duckdb
```

## Ingesting studies

```bash
# 500 most recent Phase 3 studies (and their outcomes/conditions) into raw.*
uv run endpoints pull --phase 3 --limit 500

# Multiple phases, with a date floor
uv run endpoints pull --phase "2,3" --since 2023-01-01 --limit 500

# Only studies led by a given organisation (comma-separated, OR'd)
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer"
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer,AbbVie"

# Only studies testing a given drug class (comma-separated, OR'd)
uv run endpoints pull --phase 3 --limit 500 --drug-class glp1_receptor_agonist

# Explicitly use AACT instead of the public API
uv run endpoints pull --phase 3 --limit 500 --source aact

# Skip the results section (saves warehouse size, never network)
uv run endpoints pull --phase 3 --limit 500 --no-results
```

Deliberately thin: fetch, filter, upsert. Everything downstream is
source-agnostic, which is what lets the two backends be interchangeable.
Shows a progress bar while it runs -- per API page for `--source ctgov_api`
(the eventual study count isn't known until pagination stops), per landed
table for `--source aact`.

### The two backends

Both write the same `raw.studies` / `raw.design_outcomes` shape.

| `--source` | Default? | Needs | Notes |
|---|---|---|---|
| `ctgov_api` | yes | nothing (no auth) | Public [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api). Per-outcome `population` is always NULL -- the API does not expose it. Lands `raw.browse_condition_branches` (coarse MeSH branch letters). |
| `aact` | opt-in | `.env` credentials | [AACT](https://aact.ctti-clinicaltrials.org), a Postgres mirror of ClinicalTrials.gov updated daily. Richer and faster, and avoids live-API rate limiting. Lands `raw.mesh_terms` (full MeSH tree numbers). |

The API backend is the default only because AACT access has been unreliable
(connection timeouts) here; `--source aact` is the better source when you can
reach it.

If a `--source ctgov_api` pull fails on an HTTP status, the error carries the
response body,
which is normally enough to identify a changed field or parameter name --
`src/clinical_endpoints/ingest/ctgov_api.py` is the one place to fix it. An
`--source aact` pull that cannot connect is almost always credentials or a
firewall on port 5432.

### Filtering by organisation

`--org` filters to studies whose *lead* sponsor -- never a collaborator --
matches one of the given fragments, case-insensitively:

```bash
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer"
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer,AbbVie"   # either sponsor
```

Unlike `--ta`, both backends can express this directly: the CT.gov API backend
adds `AREA[LeadSponsorName]` to `query.term`, and the AACT backend joins
`ctgov.sponsors` (filtered to `lead_or_collaborator = 'lead'`). Both apply it
server-side, *before* `--limit`, the same as `--phase`/`--since` -- so there's
none of `--ta`'s scan-cap widening to worry about, and no `vocab validate`
precondition, since `--org` doesn't touch the vocabulary at all. The matched
lead sponsor name lands in `raw.studies.organization` for every pull, whether
or not `--org` was given.

`--org` composes with `--ta` (both narrow the same pull, before `--limit`) and
follows the same upsert invariant as every other filter: it only ever affects
studies *this* pull lands, never pruning a study an earlier pull with
different filters already landed.

### AACT credentials (only for `--source aact`)

1. Register for a free read-only account at
   <https://aact.ctti-clinicaltrials.org>.
2. Copy `.env.example` to `.env` and fill it in:

   ```
   PGHOST=aact-db.ctti-clinicaltrials.org
   PGPORT=5432
   PGDATABASE=aact
   PGUSER=<your username>
   PGPASSWORD=<your password>
   ```

`.env` is gitignored. Credentials are read from the environment by DuckDB's
`postgres` extension itself (libpq conventions, via an empty `ATTACH ''`
connection string) -- never interpolated into SQL, never written to the
warehouse.

### What a pull does to what is already there

`pull` **upserts**. Studies matched by *this* pull are updated if present and
inserted if new; studies landed by an earlier pull -- different filters, a
different `--source`, whatever -- are left untouched. So:

* re-running the same filters refreshes those studies in place;
* running different filters accumulates alongside what is already there;
* the pull history in `raw._pull_log` accumulates either way.

Each pull is logged to `raw._pull_log` (`pull_id`, `pulled_at`, `source`,
`filters_json`, `source_tables`, `row_counts`); `source_tables` differs by
backend, as the table above notes.

`--replace` is the explicit opt-out of all of the above: it drops and
recreates every raw.* table this pull would otherwise upsert into, *before*
landing this pull's rows, so raw.* ends up holding only what this one pull
found -- studies from any earlier pull, with any filters or `--source`, are
gone, not just left unmatched:

```bash
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer" --replace
```

Use it when you want the warehouse to mirror exactly one set of filters
rather than accumulate across runs -- e.g. after deciding an earlier pull's
filters were wrong and you don't want its studies lingering. It is silent
about *which* studies it drops (they are simply gone, not migrated or
reported row by row) but the CLI prints one clear warning that it happened;
`raw._pull_log` still accumulates a row for the replacing pull itself, with
`"replace": true` in `filters_json`, so the fact that a replace occurred stays
in the audit trail even though its rows don't. This bypasses schema
reconciliation too (there is nothing to migrate when the table is about to be
dropped anyway), so a `--replace` pull never reports a "Migrated raw.X" line.

> `raw._pull_log.pulled_at` is a `TIMESTAMPTZ`, and DuckDB's Python client can
> only materialise one as a `datetime` if `pytz` is importable. A query of your
> own that selects it may fail with `Required module 'pytz' failed to import`.
> Select it as text (`CAST(pulled_at AS VARCHAR)`) or `pip install pytz`.
> Nothing in this project needs it -- the one place that reads the column
> renders it in SQL.

### Schema reconciliation

A warehouse outlives the release that built it, so `pull` also reconciles each
`raw.*` table against the schema the current code declares, and says what it
did:

```
Migrated raw.studies: added 11 columns (intervention_model, primary_purpose,
allocation, masking, +7 more); added PRIMARY KEY (nct_id) -- 4,812 rows preserved
```

Migration, not a refresh: dropping and re-pulling would discard every study
landed by an earlier pull with different filters, which is exactly what
upserting exists to prevent. Rows violating a newly declared key (duplicates,
NULLs) are dropped and counted; columns the current schema no longer declares
are dropped and named. Where rows cannot be carried across at all -- a value
that will not cast, or no key column to key them by -- `pull` stops and says so
rather than choosing for you, leaving the table untouched:

```
raw.studies has 4,812 rows but no nct_id column, so they cannot be keyed by the
PRIMARY KEY (nct_id) the current schema declares. Inspect the table and drop it
once you're satisfied nothing in it is worth keeping, then re-run the pull.
```

## Therapeutic areas

`pull` resolves every pulled study's therapeutic area(s) from its MeSH
conditions and interventions into `conformed.study_therapeutic_area`, as soon as
`vocab validate` has loaded the mapping. All matched areas are kept -- a
lung-cancer trial is oncology *and* respiratory -- with one marked `is_primary`
by the precedence in `therapeutic_areas.yaml`.

```bash
uv run endpoints vocab validate                              # loads the MeSH -> TA mapping
uv run endpoints pull --phase 3 --limit 500 --ta oncology
uv run endpoints pull --phase 3 --limit 500 --ta oncology,cardiovascular
```

`--ta` additionally *filters* the pull down to studies matching one of the
requested areas -- *before* `--limit` is applied, not after. Neither backend's
source (the CT.gov API's `query.term`, AACT's SQL) can express this project's
therapeutic areas directly, so `pull` fetches phase/since-matching studies
most-recent-first and keeps scanning past ones that don't match, rather than
truncating to the most recent `--limit` studies of *any* area and only then
discarding what doesn't match -- the latter would starve a smaller area
(respiratory, say) of matches it actually has, because registrations skew
heavily toward whichever conditions dominate trial activity generally
(oncology). The scan is capped (`ingest/ctgov_api.py`'s `MAX_PAGES_TA_FILTERED`,
`ingest/aact.py`'s `TA_MAX_SCANNED`); a `--ta` for a niche area combined with a
wide `--phase`/`--since` can still land fewer than `--limit` studies, and
`pull` says so when that happens. `--ta` requires `vocab validate` to have run
against this warehouse first, since it filters against the loaded mapping
rather than the YAML.

`--ta` only ever affects studies *this* pull lands: a study an earlier pull
with different filters already landed is never removed just because it
doesn't match this pull's `--ta` (the same upsert invariant every pull
honours -- see "The warehouse" above).

The mapping is layered -- intervention rules, exact descriptor overrides, MeSH
tree prefixes, descriptor regexes, defaults -- and the layers can disagree.
`ta diff-tree` runs the tree-prefix layer alone and the regex layer alone over
every pulled study's conditions and reports every disagreement, most frequent
first:

```bash
uv run endpoints ta diff-tree --out ta_tree_diff.csv
```

Each disagreement is either a wrong tree prefix or a wrong regex in
`vocab/ta_mesh_mapping.yaml`, and this diff is the only way to find them
without a MeSH expert. See
[`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md) for
the resolution order and
[`vocab/README.md`](../vocab/README.md) for the mapping itself.

## Drug classes

`pull` also resolves what each study was *testing* -- its interventions -- into
`conformed.study_drug_class`, as soon as `vocab validate` has loaded the
drug-class mapping. Like therapeutic areas, all matched classes are kept, with
one marked `is_primary` by the precedence in `drug_classes.yaml`. Unlike
therapeutic areas, every class declares a `kind`:

| kind | example | what it claims |
|---|---|---|
| `mechanism` | `glp1_receptor_agonist` | the target or pathway acted on -- the axis that carries signal for comparing endpoints |
| `pharmacologic` | `antineoplastic_agent` | the coarse action level, from CT.gov's browse branches |
| `modality` | `monoclonal_antibody` | what kind of product it is; orthogonal to the other two |
| `control` | `placebo` | a comparator arm, so it can be excluded |

A study normally carries several at once, and that is correct: pembrolizumab is
a PD-1 inhibitor *and* a monoclonal antibody, and a trial of pembrolizumab plus
carboplatin is a checkpoint-inhibitor trial *and* a platinum-chemotherapy trial.
Filter on `kind` whenever you group, or you will compare a mechanism against a
modality as though they were alternatives.

```bash
uv run endpoints vocab validate                                  # loads the drug-class mapping
uv run endpoints pull --phase 3 --limit 500 --drug-class glp1_receptor_agonist
uv run endpoints pull --phase 3 --limit 500 --drug-class sglt2_inhibitor,dpp4_inhibitor
```

`--drug-class` filters exactly as `--ta` does, and for the same reason: neither
backend can express it server-side, so `pull` scans past non-matching studies
*before* `--limit` truncates, under the same cap. It requires `vocab validate`
to have run against this warehouse first.

### What the corpus is made of, and what it missed

```bash
uv run endpoints drug-class distribution                 # every matched class
uv run endpoints drug-class distribution --kind mechanism --primary-only
uv run endpoints drug-class coverage                     # the denominator, and the review queue
```

`distribution` always prints the share of studies carrying a named class,
because a class list without its denominator is a machine for making a thin axis
look complete. `coverage` adds the breakdown by kind and lists the most frequent
interventions no layer could class -- those go to
`conformed.drug_class_review_queue` and are the input to the next vocabulary
round, exactly as `endpoints review list` is on the endpoint side.

### Where the two backends differ

The CT.gov API exposes MeSH intervention *ancestors* and coarse pharmacologic
*browse branches*; AACT publishes neither, so on an AACT pull those two layers
contribute nothing and classification rests on the curated agent names and WHO
INN stems. AACT is the stronger of the two for the **arm** tier, though: it has
a real `design_group_interventions` join table where the API offers only arm
labels to string-match. `conformed.arm_drug_class.link_method` records which
path produced each row.

```bash
uv run endpoints drug-class diff-ancestors --out drug_class_ancestor_diff.csv
```

`diff-ancestors` runs the curated layers alone and NLM's ancestry alone and
reports every disagreement -- each one is either a wrong `agent_names` entry or
a wrong `ancestor_rules` entry in `vocab/drug_class_mesh_mapping.yaml`. It
reports; it does not reconcile. On an AACT pull it will correctly say there was
nothing to diff.

See [`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md) for the layer order, what is
deliberately *not* modelled (ATC codes, chemical structure), and the four counts
a first live pull still owes this axis.

## The vocabulary

```bash
uv run endpoints vocab validate               # check, then write vocab.* tables
uv run endpoints vocab validate --check-only  # check without writing
uv run endpoints vocab validate --strict      # warnings become errors
uv run endpoints vocab validate --vocab-dir path/to/vocab
```

Validation covers id uniqueness and format, synonyms claimed by more than one
term, regex compilability, cross-file referential integrity (a `default_scale`
naming no scale, a `direction_by_ta` keyed on a therapeutic area that does not
exist), match-precedence lists that have drifted out of step with their terms,
tied precedence/priority values, and closed value sets. Errors fail the command
and write nothing; warnings are reported and do not.

Run it after any edit under `vocab/`, and before any `conform` you intend to
trust -- the pipeline reads the loaded tables, so an unvalidated edit simply
does not take effect.

### Sampling registry text for a vocabulary review

`vocab sample` exports what the registry actually says, so vocabulary terms come
from evidence rather than from imagination. It reads `raw.design_outcomes`, so
`pull` has to have run first.

```bash
# Per-field frequency table + coverage sidecar (the default)
uv run endpoints vocab sample
uv run endpoints vocab sample --min-frequency 3 --singleton-sample 500 --seed 7

# Joinable row-level sample -- what measure had this time_frame/description?
uv run endpoints vocab sample --format rows --limit 1000

# Just primary outcomes, where efficacy endpoints concentrate
uv run endpoints vocab sample --outcome-type primary
```

Every value occurring `--min-frequency` (default 2) times or more is kept
uncapped; the tail below that is a seeded random sample (`--singleton-sample`,
default 300), not an alphabetical head, so one-off endpoint wordings are fairly
represented. `vocab_review_coverage.csv` (or `<out>_coverage.csv`) reports, per
field, what fraction of rows the kept values account for -- machine-readable, so
the next review round can diff it against this one. The reasoning is in
[`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md).

## Conforming endpoints

```bash
uv run endpoints conform
```

Shows a progress bar while it runs. Each row is conformed independently, so on
a large pull the row-conforming step is parallelized across worker processes
by default once there's enough work to be worth it (`--jobs N` to pick the
worker count yourself, `--jobs 1` to force serial).

Reads `raw.design_outcomes` and the `vocab.*` tables -- never the YAML directly
-- and, for every outcome row:

1. resolves a **named endpoint** first, where the string names one outright
   (`PFS`, `OS`, `DFS`), which fixes the event and time origin definitionally;
2. runs `matching.yaml`'s **cascade** (`measure` → `description` →
   `time_frame`, `exact` → `syntactic_rule`) for form, measurement, reference
   and event;
3. falls back to **token-overlap semantic matching** for measurement -- the one
   dimension whose cascade ends in the review queue rather than in a default
   term;
4. classifies the **timepoint** (preprocessing → `not_if_matches` guards →
   patterns in priority order → named-group extraction);
5. parses the **threshold** comparator, value and unit;
6. **derives direction** from the resolved form's `direction_rule` and the
   measurement's `default_direction` / `event_polarity` -- direction is never
   matched from text.

Two vocabulary-driven disambiguation overrides apply after the plain cascade:
`forms.yaml`'s `disambiguation` (a generically ambiguous form pair, resolved on
the measurement's polarity and domain rather than on wording), and
`timepoint_patterns.yaml`'s (a baseline "through"/"up to" call, resolved by the
resolved form).

A row whose measurement does not resolve -- not even semantically -- is written
to `conformed.review_queue` and never conformed at any confidence. Everything
else lands in `conformed.endpoints`, where `form_match_method`,
`measurement_match_method`, `reference_match_method` and `event_match_method`
record whether each dimension was an `exact` hit, an inferred `syntactic_rule`,
or a `semantic` fallback, alongside per-dimension confidence and the source
field the value came from.

`conformed.endpoints.usdm_text` carries each row's rendered USDM
`SyntaxTemplate.text` -- the parameterized sentence with `<usdm:tag
name="..."/>` markup in place of the resolved values, or (for forms with no
template, like `descriptive`, or a row whose required tag still didn't
resolve) the escaped raw registry string -- so it is queryable directly by
SQL rather than only through `usdm show`. `conform` renders it with the same
code `usdm show` uses live (`usdm/project.py`'s `render_endpoint_text`), so
the two can never disagree.

`conform` replaces both tables wholesale each run -- it is a pure function of
`raw.*` plus `vocab.*`, so re-running after a vocabulary edit is the normal way
to see the effect of that edit.

### The review queue

```bash
uv run endpoints review list
uv run endpoints review list --reason measurement_unmatched --limit 50
uv run endpoints review list --status pending
```

Each entry carries the raw strings, the reason, the best semantic candidate and
its score -- so the queue doubles as the shortlist for the next vocabulary
round. Resolving entries from the CLI (`review resolve`) is
[not implemented](#not-implemented); work the queue with SQL for now.

## The results section

A `pull` also lands what each study *reported*, for the studies that posted
results -- five tables mirroring the protocol side's shape. On by default,
because the CT.gov API returns the results section inside the payload the pull
already fetches; `--no-results` opts out, and saves warehouse size rather than
network.

| table | grain | carries |
|---|---|---|
| `raw.outcome_measures` | one reported outcome | title, description, time frame, `param_type`, `dispersion_type`, `unit_of_measure` |
| `raw.outcome_groups` | one arm within one outcome | title, description, `n` |
| `raw.outcome_measurements` | one arm × class × category | `param_value`, `dispersion_value`, the interval limits |
| `raw.outcome_analyses` | one statistical comparison | the arms compared, `p_value`, effect `param_type` and value, CI, method, non-inferiority type and description |
| `raw.baseline_measurements` | one baseline characteristic × arm | title, units, `param_type`, `param_value`, `dispersion_type`, `dispersion_value`, `n` |

`raw.studies.has_results` carries the registry's own claim, whether or not the
section itself was landed.

```bash
uv run endpoints results conform
```

Conforms the reported titles through the *same* engine `conform` uses -- no
second matcher -- and writes three tables:

* **`conformed.endpoint_results`**, one row per reported outcome and per
  baseline characteristic, carrying the same dimension columns as
  `conformed.endpoints` plus a link back to the planned endpoint:
  `link_method` is `exact_title` (the reported title *is* a planned `measure`),
  `conformed_measurement` (different strings, same conformed measurement in the
  same study), or NULL.
* **`conformed.endpoint_dispersion`**, one row per arm-level measurement, with
  an `sd_estimate` and `sd_method` / `sd_is_derived` / `sd_is_approximate` /
  `sd_inputs` recording exactly how it was arrived at -- or `sd_skip_reason`
  where none could be.
* **`conformed.results_review_queue`**, for a reported title that conforms
  nowhere (`measurement_unmatched`) and for a reported outcome with no planned
  counterpart at all (`unlinked_to_planned`). Its own table, not
  `conformed.review_queue`, which `conform` replaces wholesale.

Run `conform` first, so results rows have planned endpoints to link to.

```bash
uv run endpoints results coverage
```

The four numbers this tier was gated on, measured against your warehouse: what
share of conformed studies posted results; what share of reported titles match
a planned one; the exact `param_type` / `dispersion_type` value sets in play
and which of them the vocabulary does not yet recognise; and what share of
`unit_of_measure` strings normalise against `scales.yaml`. Section 3's
"not recognised" list is the input to the next vocabulary round --
see [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md#the-gate).

## Endpoint variability

```bash
uv run endpoints stats --measurement fev1
uv run endpoints stats --measurement fev1 --form change_from_baseline --scale litres
uv run endpoints stats --measurement fev1 --ta respiratory --phase PHASE3
uv run endpoints stats --measurement fev1 --drug-class muscarinic_antagonist
uv run endpoints stats --measurement fev1 --by drug-class
uv run endpoints stats --measurement fev1 --by drug-class --analyses
uv run endpoints stats --measurement fev1 --source baseline
uv run endpoints stats --measurement fev1 --analyses
uv run endpoints stats --measurement fev1 --json
```

The empirical distribution of arm-level variability for one endpoint, across
every trial that reported a usable one.

```
measurement=fev1, source=outcome

  change_from_baseline · litres  (converted via scales.yaml)
    studies 2      arms 4      participants 778
    SD      median 0.3   IQR 0.2793-0.31   range 0.247-0.31
            reported 3 · from_inter_quartile_range 1
    timepoints  single_fixed (2)
    coverage    2 of 2 conformed studies reported a usable dispersion (100.0%)

No SD from: dispersion_type_unrecognised 1
```

**`--drug-class` filters; `--by drug-class` stratifies**, and which you want
depends on what you are asking. On the SD side the filter is the useful one
("what should I assume for FEV1 in LAMA trials"): the SD is mostly a property of
the population, the assay and the timepoint rather than the drug, so stratifying
mostly buys smaller denominators -- its real use is as a homogeneity check,
because strata that differ sharply mean the pooled number was never
exchangeable. On `--analyses` that inverts and the stratifier is the point:
pooling treatment effects across mechanisms is a category error, and a median
effect across "all drugs" has no referent. `--by drug-class` stratifies over
`mechanism` classes by default (`--by-kind` changes that) and the strata are not
disjoint -- a combination trial appears under every class it used.

`--drug-class` is the **study** tier: it narrows to trials that used the class.
It does not claim the SD came from an arm that received it. See
[`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md#class-is-an-arm-property-not-a-study-property)
for why the arm tier, which is written to `conformed.arm_drug_class`, is not
joined here yet.

One block per **(form, unit)** group, always: the SD of a change from baseline
is not the SD of a raw value, and the SD in litres is not the SD in
millilitres. `--form` and `--scale` narrow the selection; they are not needed
to make the output safe. Where `scales.yaml` declares a conversion the group is
the converted unit and says so.

| flag | what it does |
|---|---|
| `--source baseline` | baseline characteristics instead of reported outcomes -- a larger denominator and a *different quantity*, never a fallback |
| `--analyses` | effect sizes, p-values and non-inferiority margins instead of the SD distribution |
| `--only-reported` | drop every derived SD, leaving only the ones trials reported outright |
| `--no-approximate` | drop the Wan et al. IQR/range estimates, which are approximations rather than conversions |
| `--json` | the same report as JSON, including every group's coverage |

The **coverage** line is the share of conformed studies *for that endpoint*
that reported a usable dispersion. Narrowing with `--only-reported` shrinks the
numerator and leaves the denominator alone, which is the point: a `stats`
output without its denominator is a machine for producing confident numbers off
eight arms.

## Projecting to USDM 4.0

```bash
# Every endpoint in one trial, as a USDM 4.0 endpoints module
uv run endpoints usdm show NCT04162249

# A full USDM Wrapper instead, written to a file
uv run endpoints usdm show NCT04162249 --envelope wrapper -o study.json

# Just the primary endpoints, flat
uv run endpoints usdm show NCT04162249 --level primary --flatten

# Only endpoints that rendered as fully parameterized templates
uv run endpoints usdm show NCT04162249 --tier templated

# How much of the corpus renders as a fully parameterized template
uv run endpoints usdm coverage
uv run endpoints usdm coverage --limit 100
```

**Two envelopes, one projection.** `--envelope module` (the default) is USDM
class instances (`objectives[]`, `dictionaries[]`, `bcSurrogates[]`,
`analysisPopulations[]`) inside a knowledge-base envelope (`profile`, `study`,
`provenance`) whose `profile` field states that boundary machine-readably.
`--envelope wrapper` is a full, canonical USDM `Wrapper` for consumers whose
tooling only eats one; it carries no `profile` because it needs none, and names
every attribute it had to default in `provenance.synthesized[]`.

**Three fidelity tiers.** Every row in `raw.design_outcomes` becomes exactly one
USDM `Endpoint`: `templated` (every required tag resolved), `partial` (an
optional group dropped, or the form was `not_stated`), or `verbatim` (the
registry string passed through, for rows `conform` sent to the review queue). A
study whose endpoints did not conform renders less richly, never appears to have
fewer endpoints.

**Announced defaults.** `usdm coverage` also reports, per tag, how many
`templated` endpoints stand on an announced default rather than a value
resolved from the source. A default is legal at all only where the form's own
meaning entails the value (`forms.yaml`'s `reference_entailed`, checked by
`vocab validate`), never on corpus convention alone; every synthesized or
defaulted attribute carries its own `derived` flag in `extensionAttributes`
(`purpose`, `reference`, `objective`), not one blanket flag per endpoint.

**Where the decomposition goes.** Each endpoint's decomposition rides along in
`extensionAttributes`, split into what it *means* (`decomposition`) and how
confidently and by what method each dimension was decided (`conformance`).
Templates live in [`../vocab/usdm_templates.yaml`](../vocab/usdm_templates.yaml),
one per form id, validated by `vocab validate` with everything else.

Design of record: [`USDM_ENDPOINTS_API_SPEC.md`](USDM_ENDPOINTS_API_SPEC.md)
and [`USDM_PROJECTION_INTEGRITY_SPEC.md`](USDM_PROJECTION_INTEGRITY_SPEC.md).

## Serving the API

```bash
uv sync --extra serve
uv run endpoints serve --host 127.0.0.1 --port 8000
```

Read-only, and backed by the same projection as `usdm show`:

| route | returns |
|---|---|
| `GET /v4/studies/{nctId}/endpoints` | every endpoint in the study; query params `envelope=module\|wrapper`, `flatten`, `level`, `tier` |
| `GET /v4/studies/{nctId}/endpoints/coverage` | the fidelity-tier mix and defaulted-tag counts for that study |
| `GET /v4/studies/{nctId}/endpoints/{name}` | one endpoint by `name` (`END1`, `END2`, …) with its dictionary |
| `GET /v4/vocab/{dimension}/{termId}` | the vocabulary term a `BiomedicalConceptSurrogate.reference` points at |
| `GET /v4/vocab/concept/{concept}` | every measurement sharing one concept (PASI vs sPGA vs BSA) |

```bash
curl localhost:8000/v4/studies/NCT04162249/endpoints
curl "localhost:8000/v4/studies/NCT04162249/endpoints?envelope=wrapper"
curl "localhost:8000/v4/studies/NCT04162249/endpoints?level=primary&flatten=true"
curl localhost:8000/v4/vocab/measurement/pasi
```

Every response carries `X-USDM-Version` and an `ETag` -- a content hash of the
projection with the build timestamp excluded, so an unchanged warehouse always
produces the same tag. A study that was never pulled is a 404; one that was
pulled but never conformed is a 409.

## Not implemented

Three CLI commands are declared and exit with a message rather than doing
anything:

| command | status | do this instead |
|---|---|---|
| `endpoints query "<sql>"` | not implemented | `duckdb warehouse.duckdb -c "<sql>"` -- see [`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md) |
| `endpoints export --query … --format …` | not implemented | `duckdb warehouse.duckdb -c "COPY (<sql>) TO 'out.parquet'"` |
| `endpoints review resolve <id> <term>` | not implemented | edit `vocab/*.yaml`, re-run `vocab validate` and `conform` |
