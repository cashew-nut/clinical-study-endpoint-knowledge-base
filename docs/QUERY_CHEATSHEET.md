# Query cheat sheet

One page of copy-pasteable SQL for `warehouse.duckdb` once you've run the
pipeline. Everything here is read-only, ad hoc querying -- `endpoints query`/
`export` (build-order step 4) will be thin passthroughs to the same
connection; this is the escape hatch that works today.

## Run the pipeline first

```bash
uv run endpoints vocab validate                    # writes vocab.*
uv run endpoints pull --phase 3 --limit 500         # writes raw.*
uv run endpoints conform                            # writes conformed.endpoints / conformed.review_queue
```

Want a therapeutic area with a denser efficacy signal to look at, instead of
an unfiltered mix of everything on the registry?

```bash
uv run endpoints pull --phase 3 --limit 500 --ta oncology
uv run endpoints conform
```

(`--ta` needs `vocab validate` to have already run once, since it filters
against the MeSH->TA mapping `vocab validate` loads.)

## Open the warehouse from the command line

`warehouse.duckdb` is a plain DuckDB file -- no server, no separate client.
If you don't have the `duckdb` CLI, `brew install duckdb` / see
[duckdb.org/docs/installation](https://duckdb.org/docs/installation), or just
run SQL through the project's own connection:

```bash
duckdb warehouse.duckdb                             # interactive shell
duckdb warehouse.duckdb -c "select count(*) from conformed.endpoints"   # one-shot
duckdb warehouse.duckdb -c "..." -json > out.json    # scriptable, any format flag duckdb supports
uv run python -c "from clinical_endpoints.db import connect; con = connect(); print(con.execute('select 1').fetchall())"
```

Inside the interactive shell: `.tables`, `.schema conformed.endpoints`, `.mode
markdown` (or `line`/`csv`/`json`) all work as normal DuckDB CLI dot-commands.

## Sample queries

**What's in the warehouse, at a glance**

```sql
SELECT table_schema, table_name FROM information_schema.tables ORDER BY 1, 2;

SELECT (SELECT count(*) FROM raw.design_outcomes)     AS raw_rows,
       (SELECT count(*) FROM conformed.endpoints)      AS conformed,
       (SELECT count(*) FROM conformed.review_queue)   AS review_queue;
```

**Conforming coverage, and where the loss is**

```sql
-- overall conform rate
SELECT round(100.0 * (SELECT count(*) FROM conformed.endpoints) /
             (SELECT count(*) FROM raw.design_outcomes), 1) AS pct_conformed;

-- why rows landed in the review queue
SELECT reason, count(*) FROM conformed.review_queue GROUP BY 1 ORDER BY 2 DESC;

-- match strength per dimension -- exact (from `measure`) vs syntactic_rule
-- (inferred from `description`/`time_frame`) vs semantic (fuzzy fallback)
SELECT form_match_method, measurement_match_method, count(*)
FROM conformed.endpoints GROUP BY 1, 2 ORDER BY 3 DESC;
```

**The review queue -- what a human should look at first**

```sql
-- best semantic near-miss for each queued row, worst (lowest-confidence) first
SELECT nct_id, measure_raw, best_semantic_candidate, best_semantic_score
FROM conformed.review_queue
WHERE status = 'pending'
ORDER BY best_semantic_score DESC NULLS LAST
LIMIT 25;

-- or from the CLI directly:
--   uv run endpoints review list --limit 25
```

**Form / measurement / direction breakdowns**

```sql
-- what forms actually showed up, and how often each was an inference
SELECT form_id, form_match_method, count(*)
FROM conformed.endpoints GROUP BY 1, 2 ORDER BY 3 DESC;

-- the most common measurements (efficacy only)
SELECT measurement_id, count(*) AS n
FROM conformed.endpoints e
JOIN vocab.measurements m ON m.id = e.measurement_id
WHERE m.domain = 'efficacy'
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- direction sanity check: every endpoint using this measurement should agree
-- on the SAME direction only where form doesn't change it (vital_status is the
-- textbook one-measurement-three-directions case, see vocab/README.md)
SELECT measurement_id, form_id, direction_id, count(*)
FROM conformed.endpoints WHERE measurement_id = 'vital_status'
GROUP BY 1, 2, 3 ORDER BY 4 DESC;
```

**"Same measurement, different form" -- the graph layer's reason to exist**

```sql
SELECT measurement_id, string_agg(DISTINCT form_id, ', ') AS forms_seen, count(*) AS n
FROM conformed.endpoints
GROUP BY 1 HAVING count(DISTINCT form_id) > 1
ORDER BY n DESC LIMIT 15;
```

**Timepoints**

```sql
SELECT timepoint_pattern, count(*) FROM conformed.endpoints GROUP BY 1 ORDER BY 2 DESC;

-- what a classified timepoint actually extracted
SELECT time_frame_raw, timepoint_pattern, timepoint_extracted
FROM conformed.endpoints WHERE timepoint_pattern = 'baseline_to_timepoint' LIMIT 10;

-- the long tail worth reading before extending timepoint_patterns.yaml
SELECT time_frame_raw, count(*) FROM conformed.endpoints
WHERE timepoint_pattern = 'unspecified' GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
```

**Thresholds** (only populated where the matched form's `expects_threshold` is true)

```sql
SELECT measure_raw, threshold_comparator, threshold_value, threshold_unit
FROM conformed.endpoints WHERE threshold_value IS NOT NULL LIMIT 20;
```

**Joining out to therapeutic area** (needs `pull` to have run with `vocab validate` already loaded)

```sql
SELECT ta.ta_id, e.form_id, count(*)
FROM conformed.endpoints e
JOIN conformed.study_therapeutic_area ta ON ta.nct_id = e.nct_id AND ta.is_primary
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20;
```

**A single study end to end**

```sql
SELECT outcome_type, measure_raw, form_id, measurement_id, direction_id,
       reference_id, timepoint_pattern, threshold_value
FROM conformed.endpoints WHERE nct_id = 'NCT00000000' ORDER BY outcome_type;
```

## The vocabulary tables, if you want to see the contract itself

```sql
SELECT * FROM vocab.matching_cascade ORDER BY dimension, ordinal;   -- the field cascade per dimension
SELECT * FROM vocab.matching_confidence_floor;                       -- exact/syntactic_rule/semantic floors
SELECT * FROM vocab.term_precedence WHERE dimension = 'form' ORDER BY rank;
SELECT * FROM vocab.form_disambiguation;                             -- responder vs incidence, etc.
```
