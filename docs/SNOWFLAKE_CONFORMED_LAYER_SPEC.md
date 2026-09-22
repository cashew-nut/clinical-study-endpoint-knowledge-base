# Snowflake handoff: the conformed endpoint layer

> **Status: handoff spec.** Written to be implemented, not discussed. It
> describes building the `conformed` layer of this repository as Snowflake
> tables, reading AACT tables as the source of study text and dropping DuckDB
> entirely. The conformance engine itself is **not** redesigned here: the
> Python under `src/clinical_endpoints/conform/` is the behaviour of record and
> ports essentially unchanged. What this spec designs is the plumbing around it
> — configuration, the source contract, the write path, and the places where
> Snowflake will silently disagree with DuckDB.
>
> Read alongside [`CONFORMED_ERD.md`](CONFORMED_ERD.md) (grain and keys of
> every output table) and [`../vocab/README.md`](../vocab/README.md) (the
> vocabulary schema). Where this spec and the code disagree, the code wins and
> this file is stale.

---

## 0. The one-paragraph brief

Registry outcome text (`"Change from baseline in FEV1 at Week 24"`) is mapped
onto a controlled vocabulary, producing one flat star-schema row per planned
endpoint with typed foreign keys into that vocabulary, plus a review queue of
the rows that would not conform. Today that runs against a local DuckDB file
driven by a CLI. It needs to run inside Snowflake, against AACT tables already
present there, writing Snowflake tables in a database and schema the caller
names. Nothing about the *decisions* the engine makes may change: two runs of
the same vocabulary over the same registry snapshot must produce the same
`endpoint_id`s with the same dimension values.

## 1. Scope

### In scope

The nine `conformed.*` tables, the `vocab.*` tables they join to, and the
Python that writes them.

| stage | writes | depends on |
|---|---|---|
| **1. Vocabulary** | 52 `vocab.*` tables from `vocab/*.yaml` | nothing |
| **2. Planned endpoints** | `conformed.endpoints`, `conformed.review_queue` | stage 1 |
| **3. Study axes** | `conformed.study_therapeutic_area`, `conformed.study_drug_class`, `conformed.arm_drug_class`, `conformed.drug_class_review_queue` | stage 1 |
| **4. Reported results** | `conformed.endpoint_results`, `conformed.results_review_queue`, `conformed.endpoint_dispersion` | stages 1–3 |

Stage 2 is the deliverable. Stages 3 and 4 are each independently shippable
afterwards, and **stage 1 is not optional for any of them**.

Order note: stage 3's `study_therapeutic_area` feeds one input back into stage
2 (`ta_id`, for the five measurements whose direction is therapeutic-area
dependent — see §9, step 8). Stage 2 runs without it, falling back to each
measurement's unconditional default rather than failing. If you build both, run
therapeutic-area resolution first and stage 2 second, which is the order the
existing `pull` → `conform` sequence establishes.

### Out of scope — do not port

| not wanted | where it lives today |
|---|---|
| the `endpoints` CLI | `src/clinical_endpoints/cli/` |
| the HTTP API and its FastAPI deps | `src/clinical_endpoints/usdm/api.py` |
| ingestion from the CT.gov API | `src/clinical_endpoints/ingest/ctgov_api.py` |
| the AACT → `raw.*` copy pipeline | `src/clinical_endpoints/ingest/aact*.py`, `upsert.py`, `pull_log.py`, `filters.py` |
| DuckDB, the `warehouse.duckdb` file, the `postgres` ATTACH | `src/clinical_endpoints/db.py` |
| the `stats` distributions and coverage reports | `src/clinical_endpoints/results/{stats,coverage,effects}.py` |
| vocabulary review sampling | `src/clinical_endpoints/vocab/sample.py` |

The ingestion modules are still worth **reading**: §7 lifts their AACT SQL into
views, which is why no ingestion pipeline is needed.

One judgement call, flagged rather than silently made: `conformed.endpoints`
carries a `usdm_text` column rendered by `usdm/project.py`. That rendering is
pure string templating over `vocab.usdm_*` — no API, no HTTP, no schema
validation — so **keep it**; it is roughly 200 lines of already-tested code and
it is what makes the parameterised endpoint text queryable in SQL. If you want
it gone, set the column to `NULL` and skip `render_endpoint_text`; nothing else
in the layer reads it.

## 2. The three layers

```
  {SOURCE_DATABASE}.{SOURCE_SCHEMA}          AACT, read-only, not written by this pipeline
                │
                │  views  (§7)
                ▼
  {TARGET_DATABASE}.{RAW_SCHEMA}             views over AACT in the raw.* shape. No data is copied.
                │
                ├──────────────┐
                ▼              ▼
  {TARGET_DATABASE}.{VOCAB_SCHEMA}     {TARGET_DATABASE}.{CONFORMED_SCHEMA}
  the endpoint library, from YAML       the output: 9 tables, replaced wholesale
```

Three facts about this shape drive everything below.

1. **`raw` is views, not tables.** The AACT SQL that used to populate
   `raw.*` becomes the body of a view. That deletes the ingestion pipeline, the
   upsert logic and the pull log in one move, and keeps every SQL string in the
   ported modules valid.
2. **The vocabulary is the contract, and it is data.** `vocab/*.yaml` is
   validated, then materialised into tables. The engine reads the *tables*,
   never the YAML (two exceptions, §8), so a run is always against a validated
   snapshot.
3. **Every `conformed.*` table is replaced wholesale.** `CREATE OR REPLACE
   TABLE`, never a merge. These tables hold no state independent of their
   sources. Ids are `md5` content hashes, so a re-run over unchanged input
   reproduces them exactly.

## 3. Configuration

One object, and nothing else in the pipeline may hardcode a database, schema or
warehouse name.

```python
@dataclass(frozen=True)
class Layout:
    """Every physical name the pipeline knows."""

    # Where AACT already lives. Read-only; never written.
    source_database: str                       # e.g. "AACT"
    source_schema: str = "CTGOV"

    # Where this pipeline's own objects go.
    target_database: str = "CLINICAL_ENDPOINTS"
    raw_schema: str = "RAW"                    # views over AACT (§7)
    vocab_schema: str = "VOCAB"                # the endpoint library (§8)
    conformed_schema: str = "CONFORMED"        # the output (§10)

    # A SQL predicate selecting the studies in scope, over CTGOV.STUDIES
    # aliased `s` and CTGOV.DESIGNS aliased `d`. "TRUE" means the whole
    # registry; see §13 before you leave it there.
    study_filter: str = "TRUE"

    def fq(self, logical_schema: str, table: str) -> str:
        db, schema = {
            "raw": (self.target_database, self.raw_schema),
            "vocab": (self.target_database, self.vocab_schema),
            "conformed": (self.target_database, self.conformed_schema),
            "aact": (self.source_database, self.source_schema),
        }[logical_schema]
        return f'{db}.{schema}."{table.upper()}"'
```

Source it however suits your deployment — environment variables, a
`CONFIG` table, stored-procedure arguments. Two requirements:

* `study_filter` is the **only** place study selection is expressed. It
  replaces the CLI's `--phase`, `--since`, `--org`, `--limit`, `--ta` and
  `--drug-class`. Phase, date and sponsor filters are plain SQL over AACT
  (`s.phase IN ('Phase 3')`, `s.start_date >= '2020-01-01'`); therapeutic-area
  and drug-class filters are not, because they are resolved by Python, so
  filter on the *resolved* axis tables after stage 3 instead of trying to
  express them here.
* `fq()` **double-quotes the object name**. This is not stylistic; see the
  reserved-word trap in §11.

## 4. Where the Python runs

The engine is per-row pure Python over compiled regexes. Measured on this
repository's vocabulary (5,310 vocabulary rows, 242 measurement terms):

| operation | cost |
|---|---|
| `load_rules()` — build the whole rule set from `vocab.*` | 0.26 s |
| that rule set, pickled | 193 KiB |
| `conform_row()` including `usdm_text` rendering | **2.1 ms/row**, single-threaded |
| validating and materialising `vocab/*.yaml` | 5.5 s |

### Recommended: a Snowpark stored procedure

Python 3.11 runtime, `pyyaml` and `snowflake-snowpark-python` from the
Anaconda channel, the `clinical_endpoints` package staged as a zip. The
procedure queries the vocabulary tables through a Session, builds the rules,
maps over the source rows and writes the output tables. This is the shape the
existing code already has, so it is the shortest path to a correct first run,
and it keeps everything inside Snowflake.

Its ceiling is single-threaded throughput: 2.1 ms/row is ~35 s per 16k rows
and ~35 minutes per million. If `study_filter` selects a book of work rather
than the whole registry, stop here.

### Scale-out, only if you need it: a vectorized UDTF

Move `conform_row` into a Python UDTF called with `OVER (PARTITION BY nct_id)`,
so Snowflake parallelises it across the warehouse and the write becomes a
`CREATE TABLE AS SELECT`. A UDF cannot query tables, so the rules have to
arrive as a staged artifact:

* The stage-1 procedure, after writing `vocab.*`, builds `ConformRules` and
  pickles it to an internal stage, then re-creates the UDTF so its `IMPORTS`
  picks up the new file. Producing the pickle **from the stored procedure**
  rather than from a laptop is what keeps the Python version identical on both
  sides; do not pickle it anywhere else.
* The UDTF unpickles once at module scope — 0.19 s per Python process,
  amortised over the whole partition.

Do not attempt to translate the matching into SQL `RLIKE`. The patterns are
Python `re`, `matching.yaml` declares `named_groups_are_load_bearing: true`,
and the timepoint parser reads named groups out of the match object. A
translation would be a different engine with the same table names.

### Not recommended

An external Python job over `snowflake-connector-python` works and is the
easiest thing to debug locally, but it puts the compute outside Snowflake and
needs a scheduler. Keep it, if you like, as the local parity harness (§12).

## 5. What to port, file by file

| path | action |
|---|---|
| `conform/text.py` | **verbatim** |
| `conform/cascade.py` | **verbatim** |
| `conform/matcher.py` | **verbatim** |
| `conform/semantic.py` | **verbatim** |
| `conform/threshold.py` | **verbatim** — no DB access at all |
| `conform/timepoint.py` | verbatim + one array fix (§6.3) |
| `conform/direction.py` | **verbatim** |
| `conform/rules.py` | verbatim + one array fix (§6.3) |
| `conform/pipeline.py` | port `conform_row` and the two dataclasses **verbatim**; rewrite `run_conform`'s I/O ends (§6.2) and delete the `ProcessPoolExecutor` block (§11.11) |
| `usdm/{project,templates,tags,codes,ids}.py` | port `load_projection_rules` and `render_endpoint_text` and what they call; drop `envelope.py`, `api.py` and the JSON schema |
| `vocab/schema.py` | **verbatim** |
| `vocab/loader.py` | verbatim except `_insert` (§6.2) |
| `ta/resolver.py` | stage 3: port `load_ta_mapping`, `resolve_study_ta_matches`, `resolve_therapeutic_areas` verbatim; `write_study_therapeutic_area` moves onto §6.2. Drop `filter_raw_tables_by_nct_ids`, `diff_*`, `*_summary` |
| `drug_class/resolver.py` | stage 3: port `load_drug_class_mapping`, `resolve_intervention`, `resolve_study_drug_class_matches`, `resolve_drug_classes` verbatim; the four writers move onto §6.2. Drop `diff_ancestors`, `coverage_summary` |
| `results/{pipeline,dispersion,units}.py` | stage 4, as-is bar the same I/O swap |
| `db.py` | **replaced** by §6.1 + §6.2 |
| `vocab/*.yaml` | copied unchanged; it is the contract |

"Verbatim" is load-bearing, and it is achievable: every one of these modules
reaches the database through exactly one shape — `con.execute(sql[, params])`
followed by `.fetchall()` or `.fetchone()` — so the adapter in §6.1 moves all of
them without a line changed. The only writes are `db.bulk_insert`,
`vocab.loader._insert` and the four axis writers' `executemany` calls, all of
which §6.2 replaces. Resist tidying while porting: a behaviour change made in
passing is indistinguishable, in the output, from a porting bug.

## 6. The plumbing you write

Three pieces. That is the whole delta.

### 6.1 The connection adapter

```python
"""The slice of duckdb.DuckDBPyConnection the ported modules use, over Snowpark."""

from __future__ import annotations

import decimal
import re
from typing import Any, Optional, Sequence

from snowflake.snowpark import Session

_PREFIX_RE = re.compile(r"\b(raw|vocab|conformed)\.([a-z_][a-z0-9_]*)", re.IGNORECASE)
_INFO_SCHEMA_RE = re.compile(r"information_schema\.tables", re.IGNORECASE)


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> Optional[tuple]:
        return self._rows[0] if self._rows else None


class SnowflakeConnection:
    """`raw.` / `vocab.` / `conformed.` prefixes in the SQL are rewritten to the
    configured fully-qualified names, so the SQL strings in the ported modules
    need no edits."""

    def __init__(self, session: Session, layout: Layout) -> None:
        self._session = session
        self._layout = layout

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _Result:
        if _INFO_SCHEMA_RE.search(sql):
            return _Result(self._table_exists(params))
        rows = self._session.sql(
            self._qualify(sql), params=list(params or [])
        ).collect()
        return _Result([tuple(_coerce(v) for v in row) for row in rows])

    def _qualify(self, sql: str) -> str:
        return _PREFIX_RE.sub(
            lambda m: self._layout.fq(m.group(1).lower(), m.group(2)), sql
        )

    def _table_exists(self, params: Sequence[Any] | None) -> list[tuple]:
        """Every caller passes LOWER-case ('conformed', 'study_therapeutic_area').
        Snowflake's INFORMATION_SCHEMA holds names UPPER-cased and is scoped to
        one database, so the query has to be rebuilt rather than rewritten.
        `table_type` is not filtered: the raw layer is views (§7), and a view
        that does not count as existing fails the pipeline at its first step."""
        logical_schema, table = (list(params or []) + ["", ""])[:2]
        physical = {
            "raw": self._layout.raw_schema,
            "vocab": self._layout.vocab_schema,
            "conformed": self._layout.conformed_schema,
        }[str(logical_schema).lower()]
        found = self._session.sql(
            f"SELECT 1 FROM {self._layout.target_database}.information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            params=[physical.upper(), str(table).upper()],
        ).collect()
        return [tuple(row) for row in found]


def _coerce(value: Any) -> Any:
    """Snowflake returns every NUMBER as decimal.Decimal. The ported code does
    arithmetic, sorting and equality on these, and a Decimal confidence lands in
    a FLOAT column as a different value than the float it should have been."""
    if isinstance(value, decimal.Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    return value
```

That is the entire read path for all four stages.

### 6.2 The bulk writer

Two functions in the reference code write rows: `db.bulk_insert` (the
`conformed.*` fact tables, via Arrow) and `vocab.loader._insert` (the 52
vocabulary tables, via `executemany`). Replace both with one writer.

```python
def replace_table(
    session: Session,
    layout: Layout,
    logical_schema: str,
    table: str,
    ddl: str,                      # the column list, exactly as in §10
    columns: list[str],
    rows: list[tuple],
    *,
    dedupe_on: int | None = 0,     # column index of the id, or None for keyless tables
) -> int:
    """`CREATE OR REPLACE TABLE` then append. Wholesale replacement is the
    layer's contract (§2.3), and an explicit DDL keeps the column types exact —
    `save_as_table(mode="overwrite")` would re-infer them."""
    fqn = layout.fq(logical_schema, table)
    session.sql(f"CREATE OR REPLACE TABLE {fqn} ({ddl})").collect()
    if not rows:
        return 0
    if dedupe_on is not None:
        rows = list({row[dedupe_on]: row for row in rows}.values())   # §11.5
    session.create_dataframe(rows, schema=_struct_type(ddl, columns)) \
           .write.mode("append").save_as_table(fqn, column_order="name")
    return len(rows)
```

Two things to get right:

* **Pass an explicit `StructType`.** `create_dataframe` infers types from the
  data, and an all-`NULL` column — routine here: `event_id` is NULL for every
  non-event-family form — makes inference fail or produce the wrong type.
  Derive the `StructType` from the same DDL string the `CREATE` uses, so there
  is one source of truth per table.
* **`dedupe_on` is not optional.** §11.5.

For the four keyless axis tables (`study_therapeutic_area`, `study_drug_class`,
`arm_drug_class`, `drug_class_review_queue`), pass `dedupe_on=None`:
`arm_drug_class` legitimately carries duplicate-looking rows differing only in
`rule_layer` and `matched_on`.

### 6.3 The three array read sites

`vocab.timepoint_disambiguation` and `vocab.form_disambiguation` carry
`VARCHAR[]` columns. DuckDB hands those back as Python lists; Snowflake hands
back a JSON string.

```python
def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)
```

Apply at exactly three call sites:

| file | expression | becomes |
|---|---|---|
| `conform/rules.py`, `form_disambiguation` | `tuple(between)` | `tuple(as_list(between))` |
| `conform/timepoint.py`, `disambiguation` | `"between": between` | `"between": as_list(between)` |
| `conform/timepoint.py`, `disambiguation` | `set(if_form_in)` | `set(as_list(if_form_in))` |

`usdm_templates.required_tags` / `all_tags` are also arrays, but
`load_projection_rules` discards them (`_req, _all`), so they need nothing.

Why it matters is in §11.4.

## 7. The source contract: AACT

### 7.1 `raw` as views over AACT

Create the views below in `{target_database}.{raw_schema}`. Their bodies are
the AACT SQL already written in `ingest/aact.py` and `ingest/aact_results.py`,
with the `_pulled_studies` temp table replaced by `RAW.STUDY_SCOPE`. Nothing is
copied and nothing needs refreshing.

```sql
-- The one place study selection is expressed.
CREATE OR REPLACE VIEW {target}.{raw}.STUDY_SCOPE AS
SELECT s.nct_id
FROM {source}.{srcschema}.STUDIES s
LEFT JOIN {source}.{srcschema}.DESIGNS d ON d.nct_id = s.nct_id
WHERE {study_filter};

-- Lifted from ingest/aact.py::_PULLED_STUDIES_SELECT.
CREATE OR REPLACE VIEW {target}.{raw}.STUDIES AS
SELECT
    s.nct_id, s.phase, s.overall_status, s.study_type,
    s.start_date, s.primary_completion_date,
    s.brief_title, s.official_title,
    d.intervention_model, d.primary_purpose, d.allocation, d.masking,
    TRY_CAST(s.enrollment AS INTEGER) AS enrollment_count,
    s.enrollment_type,
    CASE LOWER(TRIM(e.healthy_volunteers))
        WHEN 'accepts healthy volunteers' THEN TRUE
        WHEN 'yes' THEN TRUE
        WHEN 'no'  THEN FALSE
        ELSE NULL
    END AS healthy_volunteers,
    e.gender, e.minimum_age, e.maximum_age,
    e.population AS population_description,
    sp.organization,
    (s.results_first_submitted_date IS NOT NULL) AS has_results
FROM {source}.{srcschema}.STUDIES s
JOIN {target}.{raw}.STUDY_SCOPE scope ON scope.nct_id = s.nct_id
LEFT JOIN {source}.{srcschema}.DESIGNS d      ON d.nct_id = s.nct_id
LEFT JOIN {source}.{srcschema}.ELIGIBILITIES e ON e.nct_id = s.nct_id
LEFT JOIN (
    -- ctgov.sponsors is one row per (study, lead-or-collaborator).
    SELECT nct_id, MIN(name) AS organization
    FROM {source}.{srcschema}.SPONSORS
    WHERE LOWER(lead_or_collaborator) = 'lead'
    GROUP BY nct_id
) sp ON sp.nct_id = s.nct_id;

-- The pipeline's actual input.
CREATE OR REPLACE VIEW {target}.{raw}.DESIGN_OUTCOMES AS
SELECT o.nct_id, o.outcome_type, o.measure, o.time_frame, o.description, o.population
FROM {source}.{srcschema}.DESIGN_OUTCOMES o
JOIN {target}.{raw}.STUDY_SCOPE scope ON scope.nct_id = o.nct_id;
```

### 7.2 Which AACT tables each stage needs

Stage 2 — the deliverable — needs **three**: `design_outcomes` for the rows,
and `studies` joined to `designs` for one column.

| stage | AACT table | columns used | why |
|---|---|---|---|
| 2 | `design_outcomes` | `nct_id, outcome_type, measure, time_frame, description, population` | the rows being conformed |
| 2 | `studies`, `designs` | `nct_id`; `designs.allocation` | `allocation` gates one named-endpoint rule (§9, step 4) |
| 2 | *(`eligibilities`, `sponsors`)* | as the view above selects | only to complete the `RAW.STUDIES` shape; a stage-2-only build can drop both joins and the columns they feed |
| 3 | `browse_conditions`, `browse_interventions` | `nct_id, mesh_term` | therapeutic area and the MeSH drug-class layer |
| 3 | `mesh_terms` | `mesh_term, tree_number` | the tree-prefix layer. **Zero rows in AACT as of 2026-08-19** — the pipeline degrades to the regex layer and must say so |
| 3 | `interventions` | `id, nct_id, intervention_type, name, description` | the drug-class axis. `id` orders rows; `ordinal` is `row_number() - 1` |
| 3 | `intervention_other_names` | `nct_id, intervention_id, name` | brand names and development codes; often the only string a curated agent entry matches |
| 3 | `design_group_interventions`, `design_groups` | the join table, plus `group_type, title, description` | arm-level class, via a real join (`link_method = 'join_table'`) |
| 3 | `conditions` | `nct_id, name` | free-text conditions, landed but not resolved against |
| 4 | `outcomes` | `id, nct_id, outcome_type, title, description, time_frame, population, units, units_analyzed, param_type, dispersion_type` | the reported outcomes |
| 4 | `outcome_counts` | `outcome_id, result_group_id, ctgov_group_code, scope, units, count` | the per-arm denominator |
| 4 | `outcome_measurements` | `outcome_id, ctgov_group_code, classification, category, param_value(_num), dispersion_*` | the arm-level numbers |
| 4 | `result_groups` | `id, ctgov_group_code, title, description` | arm titles |
| 4 | `outcome_analyses`, `outcome_analysis_groups` | as `ingest/aact_results.py` selects | effect sizes and p-values |
| 4 | `baseline_measurements` | as `ingest/aact_results.py` selects | baseline characteristics as their own quantity |

Two structural notes carried over from the ingestion code, both load-bearing:

* **AACT publishes no intervention MeSH ancestors and no browse branches.**
  `raw.browse_intervention_ancestors`, `raw.browse_intervention_branches` and
  `raw.browse_condition_branches` have no AACT source. Create them as empty
  views (`SELECT ... WHERE FALSE`, correctly typed) rather than omitting them:
  the resolvers check for existence and the empty case is already handled, but
  a missing object is not. Two of the drug-class resolver's five layers and one
  of the therapeutic-area resolver's layers are then inert. That is a known
  cost of the AACT source, not a defect.
* **`outcome_id` is a content hash, not AACT's `outcomes.id`,** which is not
  stable across AACT rebuilds. `ingest/results.py::outcome_id_sql` has the SQL
  form, tested against the Python one. Its `duplicate_ordinal` comes from
  `row_number() OVER (PARTITION BY nct_id, LOWER(outcome_type), title,
  time_frame ORDER BY outcomes.id)`; keep that `ORDER BY` or the ids of
  same-titled outcomes shuffle between runs.

## 8. The vocabulary layer

`vocab/*.yaml` → validate → 52 tables, ~5,310 rows, in about 5.5 seconds.
Port `vocab/loader.py` as-is apart from `_insert`; `write_vocab_tables` already
does `CREATE OR REPLACE TABLE` per table and the DDL strings are in
`_LONG_TABLES` and `DIMENSIONS`.

Run `validate_vocab` and **fail the load on any error**. It is what guarantees
id uniqueness, that no synonym is claimed by two terms, that every regex
compiles, cross-file referential integrity, and that `drug_classes.parent` is
acyclic. The engine has no defences of its own against a vocabulary that
violates these.

Type mapping for the vocabulary tables:

| in `_LONG_TABLES` / `DIMENSIONS` | Snowflake |
|---|---|
| `VARCHAR` | `VARCHAR` |
| `DOUBLE` | `FLOAT` |
| `INTEGER` | `NUMBER(38,0)` |
| `BOOLEAN` (only `usdm_templates.verbatim`) | `BOOLEAN` |
| `VARCHAR[]` (4 columns) | `ARRAY` — and read through §6.3 |
| `TIMESTAMP` (`_load_log.loaded_at`) | `TIMESTAMP_NTZ` |

Two exceptions to "the engine reads tables, never YAML": `load_ta_mapping` and
`load_drug_class_mapping` read their `defaults:` block from the YAML file,
because it is not persisted to `vocab.*`. So stage 3 needs the YAML reachable
at runtime — stage the `vocab/` directory, or promote those two defaults
(`no_pattern_matched`, `no_mesh_terms_on_study`, and the drug-class
equivalents) into `Layout`. Either is fine; leaving `default_vocab_dir()` to
walk a filesystem that does not exist inside a stored procedure is not.

`vocab._load_log` is the one table that appends rather than replaces
(`CREATE TABLE IF NOT EXISTS`): it is the load history, and
`usdm/project.py::_vocab_version` reads `max(loaded_at)` from it to stamp the
projection. Keep that behaviour.

## 9. The conformance algorithm

Ported code, stated here so a parity failure can be localised without reading
all of it. Per `raw.design_outcomes` row, in this order:

1. **Normalise** `measure`, `description`, `time_frame` through the steps in
   `vocab.matching_normalisation`, in `ordinal` order. An unimplemented step
   name raises rather than being skipped.
2. **Named endpoint.** Match `named_endpoints.yaml` over `measure` then
   `description`. A hit is a *fallback source* for four dimensions, never an
   override: it fills only where that dimension's own cascade came back silent.
3. **Measurement.** Cascade `measure` (`exact`) → `description`
   (`syntactic_rule`). Then the named-endpoint `default_measurement`. Then a
   token-overlap semantic fallback (≥0.6 coverage of the term's own vocabulary,
   ≥2 shared content words). Still nothing → **the row goes to
   `conformed.review_queue`, reason `measurement_unmatched`, and stops here.**
   Measurement is the one dimension allowed no default. The queued row's
   `best_semantic_candidate` / `best_semantic_score` are recomputed with the
   thresholds dropped (`min_score=0.0`, `min_overlap=1`), so they report the
   closest term rather than an acceptable one; that is why a queued row can
   carry a candidate it was not conformed to.
4. **Reference.** Cascade `time_frame` → `measure` → `description`, falling
   back to `not_stated`. The named-endpoint reference fills a silent cascade
   *only on a randomised study* (`raw.studies.allocation` starts with
   "random"): asserting "from randomisation" on a single-arm trial would be an
   unannounced default.
5. **Form.** Cascade `measure` → `description` → `time_frame` (where a
   reference matched from `time_frame` implies its typical form, first in file
   order), falling back to `not_stated`. Then `forms.yaml`'s disambiguation
   block, which only fires when the other member of a declared pair matches the
   same text.
6. **Timepoint.** Preprocess `time_frame`, then guards, then patterns in
   `priority` order, extracting named groups. Then the resolved form may
   override a `baseline_to_timepoint` / `cumulative_window` call when the string
   carries a "through" / "up to" connective.
7. **Event**, only for forms with `event_family = 'true'`. Named-endpoint event,
   then `events.yaml` over the cascade fields, then the measurement's
   `implies_event`, then `not_stated`. For every other form `event_id` is
   **NULL, and the NULL means "this endpoint has no event"** — not "the event
   went unresolved".
8. **Direction, derived and never matched.** Polarity comes from the resolved
   event, then `directions.yaml`'s free-text cues, then the measurement's own
   `event_polarity`. The form's `direction_rule` then maps it. For
   `inherit_measurement` the measurement's `default_direction` applies, with a
   per-therapeutic-area override where one is declared — five measurements have
   one (`body_weight`, `body_mass_index`, and the three blood-pressure terms).
   Without stage 3, `ta_id` is NULL and those five fall back to their
   unconditional default.
9. **Threshold**, only for forms with `expects_threshold = 'true'`: parse
   `measure`, then `description` if that found nothing.
10. **Provenance.** Every dimension records `*_match_method` (`exact`,
    `syntactic_rule`, `semantic`, `named_endpoint`, `implied`, or NULL for a
    fallback), `*_confidence` from `vocab.matching_confidence_floor`, and
    `*_source_field`.

The cascade is data, in `vocab.matching_cascade`, not code:

| dimension | fields, in order | fallback |
|---|---|---|
| `named_endpoint` | `measure`, `description` | `not_matched` |
| `measurement` | `measure`, `description` | **`review_queue`** |
| `form` | `measure`, `description`, `time_frame` | `not_stated` |
| `reference` | `time_frame`, `measure`, `description` | `not_stated` |
| `event` | `measure`, `description` | `not_stated` |
| `timepoint` | `time_frame` | `unspecified` |

`endpoint_id = md5(nct_id|outcome_type|measure|time_frame|description)`, with
each component replaced by `""` when NULL. A queued row's `review_id` is the
`endpoint_id` it would have had.

## 10. Target DDL

Mechanical translation of the `CREATE OR REPLACE TABLE` statements in
`conform/pipeline.py`, `results/pipeline.py`, `ta/resolver.py` and
`drug_class/resolver.py`. Type mapping: `DOUBLE`→`FLOAT`,
`INTEGER`→`NUMBER(38,0)`, `TIMESTAMP`→`TIMESTAMP_NTZ`, `JSON`→`VARCHAR`
(see §11.6), `VARCHAR` and `BOOLEAN` unchanged.

`PRIMARY KEY` is kept for documentation. **Snowflake does not enforce it** —
see §11.5.

```sql
CREATE OR REPLACE TABLE {conformed}.ENDPOINTS (
    endpoint_id VARCHAR PRIMARY KEY,
    nct_id VARCHAR, outcome_type VARCHAR,
    measure_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR, population VARCHAR,
    form_id VARCHAR, form_match_method VARCHAR, form_confidence FLOAT, form_source_field VARCHAR,
    measurement_id VARCHAR, measurement_match_method VARCHAR, measurement_confidence FLOAT,
    measurement_source_field VARCHAR,
    reference_id VARCHAR, reference_match_method VARCHAR, reference_confidence FLOAT,
    reference_source_field VARCHAR,
    event_id VARCHAR, event_match_method VARCHAR, event_confidence FLOAT, event_source_field VARCHAR,
    named_endpoint_id VARCHAR,
    direction_id VARCHAR, event_polarity_used VARCHAR,
    scale_id VARCHAR,
    timepoint_pattern VARCHAR, timepoint_raw VARCHAR, timepoint_match_method VARCHAR,
    timepoint_extracted VARCHAR,          -- JSON text; §11.6
    threshold_comparator VARCHAR, threshold_value FLOAT, threshold_unit VARCHAR,
    analysable BOOLEAN,
    usdm_text VARCHAR,
    conformed_at TIMESTAMP_NTZ
);

CREATE OR REPLACE TABLE {conformed}.REVIEW_QUEUE (
    review_id VARCHAR PRIMARY KEY,
    nct_id VARCHAR, outcome_type VARCHAR,
    measure_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR, population VARCHAR,
    reason VARCHAR,
    candidate_form_id VARCHAR, candidate_direction_id VARCHAR,
    best_semantic_candidate VARCHAR, best_semantic_score FLOAT,
    status VARCHAR,                       -- always 'pending'; nothing writes it back
    queued_at TIMESTAMP_NTZ
);

-- Stage 3.
CREATE OR REPLACE TABLE {conformed}.STUDY_THERAPEUTIC_AREA (
    nct_id VARCHAR, ta_id VARCHAR, rule_layer VARCHAR, matched_on VARCHAR, is_primary BOOLEAN
);

CREATE OR REPLACE TABLE {conformed}.STUDY_DRUG_CLASS (
    nct_id VARCHAR, drug_class_id VARCHAR, kind VARCHAR,
    rule_layer VARCHAR, matched_on VARCHAR, is_primary BOOLEAN
);

CREATE OR REPLACE TABLE {conformed}.ARM_DRUG_CLASS (
    nct_id VARCHAR, group_title VARCHAR, drug_class_id VARCHAR, kind VARCHAR,
    rule_layer VARCHAR, matched_on VARCHAR, link_method VARCHAR
);

CREATE OR REPLACE TABLE {conformed}.DRUG_CLASS_REVIEW_QUEUE (
    nct_id VARCHAR, ordinal NUMBER(38,0), name VARCHAR, mesh_term VARCHAR, reason VARCHAR
);

-- Stage 4. The dimension block is identical to ENDPOINTS, name for name, so a
-- query written against the planned half runs unchanged against the reported
-- half. tests/test_results_conform.py asserts that; keep it true.
CREATE OR REPLACE TABLE {conformed}.ENDPOINT_RESULTS (
    result_id VARCHAR PRIMARY KEY,
    result_kind VARCHAR,                  -- 'outcome' | 'baseline'
    source_id VARCHAR,                    -- polymorphic: read only with result_kind
    nct_id VARCHAR, outcome_type VARCHAR,
    measure_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR, population VARCHAR,
    form_id VARCHAR, form_match_method VARCHAR, form_confidence FLOAT, form_source_field VARCHAR,
    measurement_id VARCHAR, measurement_match_method VARCHAR, measurement_confidence FLOAT,
    measurement_source_field VARCHAR,
    reference_id VARCHAR, reference_match_method VARCHAR, reference_confidence FLOAT,
    reference_source_field VARCHAR,
    event_id VARCHAR, event_match_method VARCHAR, event_confidence FLOAT, event_source_field VARCHAR,
    named_endpoint_id VARCHAR,
    direction_id VARCHAR, event_polarity_used VARCHAR,
    scale_id VARCHAR,
    timepoint_pattern VARCHAR, timepoint_raw VARCHAR, timepoint_match_method VARCHAR,
    timepoint_extracted VARCHAR,
    threshold_comparator VARCHAR, threshold_value FLOAT, threshold_unit VARCHAR,
    analysable BOOLEAN,
    link_method VARCHAR,                  -- 'exact_title' | 'conformed_measurement' | NULL
    planned_endpoint_id VARCHAR,
    link_agrees_on_form BOOLEAN,
    conformed_at TIMESTAMP_NTZ
);

CREATE OR REPLACE TABLE {conformed}.RESULTS_REVIEW_QUEUE (
    review_id VARCHAR PRIMARY KEY,        -- equals ENDPOINT_RESULTS.result_id
    result_kind VARCHAR, source_id VARCHAR,
    nct_id VARCHAR, outcome_type VARCHAR,
    title_raw VARCHAR, description_raw VARCHAR, time_frame_raw VARCHAR,
    reason VARCHAR,                       -- 'measurement_unmatched' | 'unlinked_to_planned'
    measurement_id VARCHAR,
    best_semantic_candidate VARCHAR, best_semantic_score FLOAT,
    status VARCHAR, queued_at TIMESTAMP_NTZ
);

CREATE OR REPLACE TABLE {conformed}.ENDPOINT_DISPERSION (
    dispersion_id VARCHAR PRIMARY KEY,
    result_id VARCHAR, result_kind VARCHAR, source_id VARCHAR, nct_id VARCHAR,
    group_key VARCHAR, group_title VARCHAR,
    class_title VARCHAR, category_title VARCHAR,
    param_type_raw VARCHAR, param_kind VARCHAR,
    dispersion_type_raw VARCHAR, dispersion_kind VARCHAR, confidence_percent FLOAT,
    unit_raw VARCHAR, scale_id VARCHAR, scale_match_method VARCHAR,
    n NUMBER(38,0), n_source VARCHAR,
    central_value FLOAT,
    sd_estimate FLOAT, sd_method VARCHAR, sd_is_derived BOOLEAN, sd_is_approximate BOOLEAN,
    sd_scale VARCHAR, sd_skip_reason VARCHAR, sd_inputs VARCHAR,   -- JSON text
    sd_estimate_si FLOAT, si_scale_id VARCHAR,
    computed_at TIMESTAMP_NTZ
);
```

Optional and cheap: a thin view per table exposing
`TRY_PARSE_JSON(timepoint_extracted)` / `TRY_PARSE_JSON(sd_inputs)` as VARIANT,
for downstream consumers who want dot-notation.

## 11. Traps

Each of these is a way for the Snowflake build to produce plausible, wrong
output without raising. They are ordered by how quietly they fail.

**11.1 Vocabulary booleans are the *strings* `'true'` / `'false'`.**
`vocab/loader.py::_scalar` writes Python bools as lowercase strings, and the
engine compares them as strings:

```python
form_analysable = {row[0]: (row[1] == "true") for row in con.execute(
    "SELECT id, analysable FROM vocab.forms").fetchall()}
```

Declare `analysable`, `expects_threshold`, `event_family`, `composite` and
`reference_entailed` as **VARCHAR**. Make them Snowflake `BOOLEAN` and every
one of those comparisons yields `False` forever: no form is event-family, so no
endpoint ever resolves an event; no form expects a threshold, so no threshold is
ever parsed; nothing raises. (`usdm_templates.verbatim` is the one genuine
BOOLEAN, read as `bool(verbatim)`.)

**11.2 `information_schema` is per-database and upper-cased.** Every
`_table_exists` call passes lowercase names. Unhandled, `_table_exists(con,
"raw", "design_outcomes")` returns False and stage 2 aborts with "raw.design_outcomes
is empty"; `_table_exists(con, "conformed", "study_therapeutic_area")` returns
False and the therapeutic-area direction overrides silently stop applying.
Handled in §6.1 — do not drop that branch.

**11.3 `NUMBER` comes back as `decimal.Decimal`.** Confidence floors,
priorities, ordinals and precedences all arrive as Decimals. Some are already
cast (`int(priority)`); others are sorted on, arithmetically combined, or
written straight into a FLOAT column. Coerce once, in the adapter (§6.1).

**11.4 Array columns arrive as JSON strings, and the code iterates them.**
`tuple(between)` over the string `'["responder_proportion","incidence_proportion"]'`
gives a tuple of 47 single characters. No exception; the disambiguation rule
simply never matches anything again, and `responder_proportion` vs
`incidence_proportion` starts being decided by raw precedence. Fix at the three
sites in §6.3.

**11.5 Snowflake does not enforce `PRIMARY KEY`.** DuckDB does, and the
reference pipeline leans on it: two byte-identical `design_outcomes` rows hash
to one `endpoint_id`, and DuckDB raises rather than storing both. In Snowflake
the same input silently produces two identical rows, and every downstream
`count(*)` is wrong. Dedupe by id before writing (§6.2) and assert
`count(*) = count(DISTINCT <id>)` afterwards (§12).

**11.6 `JSON` is not a Snowflake type.** `timepoint_extracted` and `sd_inputs`
hold `json.dumps(...)` output. Store them as `VARCHAR` — the text round-trips
byte-for-byte, which is what makes the parity check in §12 a string comparison.
Using `VARIANT` means routing every write through `PARSE_JSON` and comparing
parsed structures instead; worth it only if you need dot-notation in the
physical table rather than in a view.

**11.7 `outcome_type` is not case-normalised, anywhere.** AACT writes
`Primary`; the CT.gov API wrote `primary`. It is stored verbatim on all four
row-level tables. Snowflake string comparison is case-sensitive, so
`WHERE outcome_type = 'PRIMARY'` returns nothing. Do not "fix" it by folding the
column: it is part of the `endpoint_id` hash input, and folding it renumbers
every id. Compare case-insensitively in queries instead.

**11.8 `md5` inputs must coalesce.** `_row_id` joins `x or ""`, mapping both
NULL and `''` to empty. In SQL, `||` with a NULL yields NULL, so any SQL-side
id must `COALESCE(x, '')` every component — `ingest/results.py::outcome_id_sql`
shows the pattern. Prefer computing ids in Python, where the reference
implementation already does.

**11.9 Reserved words.** Two vocabulary tables are named after SQL keywords:
`vocab.references` (`REFERENCES` is on Snowflake's reserved list) and
`vocab.schema`. Confirm against your own account how each behaves unquoted, but
the question is moot if you keep the quoting: `fq()` double-quotes and
upper-cases every object name, making them `"REFERENCES"` and `"SCHEMA"`, which
is addressable whatever the keyword status. Dropping the quoting means renaming
the tables and editing every SQL string that names them — the more expensive of
the two paths.

**11.10 Matching is order-dependent, and the order is in the data.**
Dimensions with a `match_precedence` are first-match-wins over
`vocab.term_precedence`; dimensions without are longest-match-wins, ties broken
on **YAML file order** via `vocab.term_order`. Both tables must load, and
`ORDER BY` must be preserved in the queries that read them. Reordering terms in
a YAML file is a behaviour change.

**11.11 `ProcessPoolExecutor` cannot run in a Snowflake stored procedure.**
Delete the parallel branch in `run_conform` and keep the serial path; §4 is how
you get parallelism back if you need it.

**11.12 Timestamps are naive UTC.** The code writes
`datetime.now(timezone.utc).replace(tzinfo=None)` into `TIMESTAMP_NTZ`. Do not
substitute `CURRENT_TIMESTAMP()`, which is session-timezone dependent and would
make `conformed_at` vary by who ran the job.

**11.13 `casefold_unless_case_sensitive` is deliberately a no-op.** Text stays
case-preserved and each compiled regex carries its own case sensitivity, which
is the only way `matching.yaml`'s rule "all-caps synonyms of ≤5 characters match
case-sensitively" can be honoured on one prepared string. It looks like dead
code. It is not; "PFS" would start matching "pfs" in prose.

**11.14 `= ANY(?)` is a DuckDB form with no Snowflake equivalent.** It appears
in the stage-3 resolvers, in the `WHERE` clause they build when a caller passes
an explicit `nct_ids` list (`ta/resolver.py::resolve_therapeutic_areas`,
`_condition_tree_numbers`, `_condition_branch_tree_candidates`;
`drug_class/resolver.py::_where`). Do not translate it: **call those functions
with `nct_ids=None`.** The predicate then collapses to empty and study scope
comes from `RAW.STUDY_SCOPE` (§7.1), which is where the configuration says it
belongs. Nothing else in the ported code uses the form.

**11.15 `link_method` non-NULL with `planned_endpoint_id` NULL is correct.**
Stage 4 only. It means the reported title matched a planned outcome verbatim,
but that planned outcome went to `review_queue`, so the link is real and the
target is not there to point at. Do not add a constraint against it. Four more
edges like this are in
[`CONFORMED_ERD.md`](CONFORMED_ERD.md#five-edges-that-are-not-what-they-look-like);
read that section before enforcing anything.

## 12. Acceptance criteria

### Parity against the reference implementation

The repository ships the golden fixtures to test against.

1. `tests/test_vocab_loader.py::REFERENCE_TABLE_FIXTURES` — 15 registry
   strings with their expected `(form_id, measurement_id, direction_id)`.
   `tests/test_conform.py::test_reference_table_fixtures_conform_end_to_end`
   additionally asserts each one's `match_method`. Every one must come out of
   Snowflake identical, with zero rows in `review_queue`.
2. `tests/test_vocab_loader.py::TIMEPOINT_FIXTURES` — 20 `time_frame` strings
   with their expected pattern. Coverage must stay at or above the
   `coverage_on_sample` baseline recorded in `vocab/timepoint_patterns.yaml`.
3. `tests/test_conform.py` has 70 passing tests against an in-memory DuckDB
   fixture warehouse, including two real studies conformed end to end
   (`NCT01777919`'s PFS and OS rows). Port the assertions; the fixtures are
   plain SQL inserts.

Then the check that actually catches porting bugs:

4. **Full-corpus row-level diff.** Pick a `study_filter` narrow enough to run
   locally (a few thousand studies). Run the unmodified DuckDB pipeline over
   the same AACT snapshot, export `conformed.endpoints` and
   `conformed.review_queue`, and diff every column of every row against the
   Snowflake output, joined on `endpoint_id`. **Require zero differences.** The
   ids are content hashes, so this join is exact and a shifted or
   re-typed column shows up immediately. Run it once per stage.

### Invariants, as SQL, after every run

```sql
-- 1. Every distinct source row lands exactly once, in exactly one table.
--    It counts DISTINCT hashes, not rows, because byte-identical source rows
--    share an endpoint_id and collapse onto one output row by design (§11.5).
--    The hash expression below is verified to agree with conform/pipeline.py's
--    `_row_id` on NULL, empty-string and populated inputs, so this doubles as
--    the check on §11.8.
WITH source AS (
    SELECT DISTINCT md5(
        coalesce(nct_id, '')       || '|' || coalesce(outcome_type, '') || '|' ||
        coalesce(measure, '')      || '|' || coalesce(time_frame, '')   || '|' ||
        coalesce(description, '')
    ) AS endpoint_id
    FROM {raw}.DESIGN_OUTCOMES
)
SELECT (SELECT count(*) FROM source)
     - (SELECT count(*) FROM {conformed}.ENDPOINTS)
     - (SELECT count(*) FROM {conformed}.REVIEW_QUEUE) AS must_be_zero;

-- 2. No id fan-out (§11.5). Both must be zero.
SELECT count(*) - count(DISTINCT endpoint_id) FROM {conformed}.ENDPOINTS;
SELECT count(*) - count(DISTINCT review_id)   FROM {conformed}.REVIEW_QUEUE;

-- 3. The two tables share an id space and must not overlap.
SELECT count(*) FROM {conformed}.ENDPOINTS e
JOIN {conformed}.REVIEW_QUEUE r ON r.review_id = e.endpoint_id;

-- 4. No dangling vocabulary references. Repeat per dimension; all must be zero.
--    For the nullable ones (event_id, named_endpoint_id, scale_id) add
--    `AND e.<column> IS NOT NULL`, or a legitimate NULL reads as dangling.
SELECT count(*) FROM {conformed}.ENDPOINTS e
LEFT JOIN {vocab}."MEASUREMENTS" m ON m.id = e.measurement_id
WHERE m.id IS NULL;

-- 5. event_id is NULL exactly when the form is not event-family (§9, step 7).
--    An event-family form that resolved nothing carries 'not_stated', never NULL.
--    COALESCE because a three-valued <> silently counts nothing.
SELECT count(*) FROM {conformed}.ENDPOINTS e
JOIN {vocab}."FORMS" f ON f.id = e.form_id
WHERE (coalesce(f.event_family, 'false') = 'true') <> (e.event_id IS NOT NULL);

-- 6. The boolean-as-string trap (§11.1), caught directly: if this is zero on a
--    corpus with any time-to-event endpoints, forms.event_family is broken.
SELECT count(*) FROM {conformed}.ENDPOINTS WHERE event_id IS NOT NULL;

-- 7. Nothing conformed at a confidence below the floor for its method.
SELECT count(*) FROM {conformed}.ENDPOINTS e
JOIN {vocab}."MATCHING_CONFIDENCE_FLOOR" c ON c.match_method = e.measurement_match_method
WHERE e.measurement_confidence < c.confidence;

-- 8. Idempotence. Run the pipeline twice over an unchanged source and diff.
--    Only conformed_at may differ.
```

Report, per run: rows in, rows conformed, rows queued by reason, and the
`match_method` distribution per dimension. A drop in the conformed share
between vocabulary revisions is the signal the review queue exists to carry.

## 13. Performance and sizing

At 2.1 ms/row single-threaded (§4), the stored-procedure path costs roughly a
minute per 30,000 planned outcomes. AACT's `design_outcomes` runs to well over
a million rows for the whole registry, so a full-registry single-threaded run
is on the order of an hour. Three levers, in the order to reach for them:

1. **`study_filter`.** Most uses of this layer want a book of work — a phase, a
   date range, a therapeutic area — not the registry. This is free.
2. **The UDTF (§4)**, partitioned by `nct_id`. Scales with the warehouse.
3. **Warehouse size** only after (2): the stored-procedure path is one Python
   process and a bigger warehouse does not make it faster.

Do not cache `ConformRules` across vocabulary revisions without keying the
cache on `vocab._load_log`'s `max(loaded_at)`. Stale rules with fresh ids is
the one failure mode here that survives a re-run.

## 14. Licence

This repository is Apache 2.0. Code lifted into an internal Snowflake
implementation stays under it: retain the `LICENSE` file and the copyright
notice with the copied sources, and note that the files were modified. The
`vocab/*.yaml` library is covered by the same licence and is the part most
worth keeping in sync with upstream — it is where the curation lives.

## Standing constraints

Decisions a later change should not quietly undo.

* **The vocabulary is data, loaded from validated YAML.** Editing a term in a
  `vocab.*` table directly, or hardcoding a term id in the pipeline, breaks the
  guarantee that a run is reproducible from `vocab/`.
* **Measurement gets no default.** A row whose measurement resolves nowhere
  goes to the review queue. Conforming it at low confidence to keep a coverage
  number up destroys the property the layer exists for.
* **Direction is derived, never matched.** It comes from the form's
  `direction_rule` and the measurement's polarity, which is how "overall
  survival" and "mortality rate" get opposite directions without either string
  saying so.
* **Ids are content hashes over registry text.** Never a sequence, never a
  surrogate. This is what makes two warehouses built from the same snapshot
  comparable, and what makes the parity diff in §12 possible.
* **`conformed.*` is replaced wholesale, never merged.** It is derived; it
  holds no state worth preserving independently of `vocab.*` and the source.
* **Provenance travels with every dimension.** `*_match_method`,
  `*_confidence`, `*_source_field`. A wrong answer has to stay auditable and a
  low-confidence one filterable.
