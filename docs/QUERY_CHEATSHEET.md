# Query cheat sheet

One page of copy-pasteable SQL for `warehouse.duckdb` once you've run the
pipeline. Everything here is read-only, ad hoc querying. (`endpoints query` and
`endpoints export` are declared but not implemented -- see
[`USAGE.md`](USAGE.md#not-implemented); the `duckdb` CLI below is what works
today.)

## Run the pipeline first

```bash
uv run endpoints vocab validate                    # writes vocab.*
uv run endpoints pull --phase 3 --limit 500         # writes raw.* (incl. the results section)
uv run endpoints conform                            # writes conformed.endpoints / conformed.review_queue
uv run endpoints results conform                    # writes conformed.endpoint_results / endpoint_dispersion
```

(`pull` also writes `conformed.study_therapeutic_area` and
`conformed.study_drug_class` on its way through, as soon as `vocab validate`
has loaded the two mappings.)

Want a therapeutic area with a denser efficacy signal to look at, instead of
an unfiltered mix of everything on the registry?

```bash
uv run endpoints pull --phase 3 --limit 500 --ta oncology
uv run endpoints conform
```

(`--ta` needs `vocab validate` to have already run once, since it filters
against the MeSH->TA mapping `vocab validate` loads.)

Or just one sponsor's studies -- `--org` filters to the *lead* sponsor, no
`vocab validate` needed:

```bash
uv run endpoints pull --phase 3 --limit 500 --org "Pfizer"
select nct_id, brief_title, organization from raw.studies order by start_date desc;
```

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

**The parameterized endpoint text**

`usdm_text` is the USDM `SyntaxTemplate.text` for each row -- tags unresolved,
e.g. `<p>Time to <usdm:tag name="event"/> <usdm:tag name="timepoint"/></p>` --
rendered by `conform` itself
([`USDM_ENDPOINTS_API_SPEC.md`](USDM_ENDPOINTS_API_SPEC.md#which-attribute-carries-what)),
so it doesn't require calling `endpoints usdm show` to inspect:

```sql
SELECT nct_id, measure_raw, usdm_text FROM conformed.endpoints
WHERE nct_id = 'NCT04162249' ORDER BY outcome_type;

-- endpoints still stuck at verbatim tier within conformed.endpoints (no
-- <usdm:tag> markup at all) -- usually a form with no template (`descriptive`)
-- or an event-family row whose event didn't resolve
SELECT nct_id, form_id, measure_raw FROM conformed.endpoints
WHERE usdm_text NOT LIKE '%<usdm:tag%' LIMIT 20;
```

## Cross-study comparability

The reason the warehouse exists: which studies measured the same thing a
different way. Every one of these is a join on `conformed.endpoints` and its
foreign keys -- which is why they live here as SQL rather than as a separate
node/edge encoding of the same facts. See
[`COMPOSITE_ENDPOINTS_SPEC.md`](COMPOSITE_ENDPOINTS_SPEC.md) for the one
relation in this domain that recursion *would* serve better, and why it is not
built.

**Read the caveat below first.** These comparisons only see rows that conformed,
which is a biased subset.

**Same measurement, different form**

Same underlying quantity, different kind of number derived from it -- overall
survival as a survival distribution vs. a 2-year landmark rate vs. 30-day
mortality. The first query finds the measurements worth looking at; the second
opens one up.

```sql
-- which measurements were expressed as more than one form, in more than one study
SELECT measurement_id,
       count(DISTINCT form_id) AS forms,
       count(DISTINCT nct_id)  AS studies,
       string_agg(DISTINCT form_id, ', ') AS forms_seen
FROM conformed.endpoints
GROUP BY 1
HAVING count(DISTINCT form_id) > 1 AND count(DISTINCT nct_id) > 1
ORDER BY forms DESC, studies DESC
LIMIT 15;

-- then the study-level pairs behind one of them.
-- `b.form_id > a.form_id` both forces the forms to differ and keeps each pair
-- once instead of twice; keep the measurement_id filter, since without it this
-- is a self-join that is quadratic in the largest measurement group.
SELECT a.measurement_id,
       a.form_id AS form_a, a.nct_id AS study_a, a.direction_id AS direction_a,
       b.form_id AS form_b, b.nct_id AS study_b, b.direction_id AS direction_b
FROM conformed.endpoints a
JOIN conformed.endpoints b
  ON b.measurement_id = a.measurement_id
 AND b.form_id > a.form_id
 AND b.nct_id <> a.nct_id
WHERE a.measurement_id = 'vital_status'
ORDER BY form_a, form_b
LIMIT 50;
```

Differing `direction_id` down those pairs is the point, not a bug: the same
measurement under a different form genuinely reverses which way is better (see
`vocab/README.md`, "Direction is derived").

**Same concept, different instrument**

The harder question, and the one the form comparison above can't reach: two
trials both measured psoriasis severity, one with PASI and one with sPGA.
`measurements.yaml` names instruments at instrument level and carries a
`concept` for exactly this.

```sql
-- concepts measured with more than one instrument across the corpus
SELECT m.concept,
       count(DISTINCT e.measurement_id) AS instruments,
       count(DISTINCT e.nct_id)         AS studies,
       string_agg(DISTINCT e.measurement_id, ', ') AS instruments_seen
FROM conformed.endpoints e
JOIN vocab.measurements m ON m.id = e.measurement_id
WHERE m.concept IS NOT NULL
GROUP BY 1
HAVING count(DISTINCT e.measurement_id) > 1
ORDER BY instruments DESC, studies DESC
LIMIT 15;

-- study-level pairs within one concept (same bounding rule as above)
SELECT ma.concept,
       a.measurement_id AS instrument_a, a.form_id AS form_a, a.nct_id AS study_a,
       b.measurement_id AS instrument_b, b.form_id AS form_b, b.nct_id AS study_b
FROM conformed.endpoints a
JOIN vocab.measurements ma ON ma.id = a.measurement_id
JOIN conformed.endpoints b  ON b.nct_id <> a.nct_id
JOIN vocab.measurements mb  ON mb.id = b.measurement_id AND mb.concept = ma.concept
WHERE ma.concept = 'psoriasis_severity'
  AND b.measurement_id > a.measurement_id
ORDER BY study_a, study_b
LIMIT 50;
```

**Thresholds: convention or one-off**

A `>= 30%` decrease that recurs identically across many oncology studies, every
one an `exact` match, is RECIST -- a convention. A threshold appearing in one
study behind a `semantic` match is not. `measurement_match_method` is what
separates them.

```sql
SELECT e.measurement_id, e.form_id,
       e.threshold_comparator, e.threshold_value, e.threshold_unit,
       count(DISTINCT e.nct_id) AS studies,
       count(*) FILTER (WHERE e.measurement_match_method = 'exact') AS exact_matches,
       round(avg(e.measurement_confidence), 2) AS mean_confidence
FROM conformed.endpoints e
WHERE e.threshold_value IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
ORDER BY studies DESC, exact_matches DESC
LIMIT 25;
```

**The caveat: what these comparisons cannot see**

A row whose measurement doesn't resolve goes to `conformed.review_queue` and
never becomes comparable to anything. Round-two measurement coverage was 64.5%,
and 77% of `measure` strings occur exactly once -- so the queries above are
weighted toward the head of the corpus (OS, PFS, ORR, adverse events), and an
unusual instrument used in two trials is disproportionately likely to be missing
altogether. Absence of a link is not evidence that no link exists. Check the
denominator before drawing a conclusion from any of the above:

```sql
SELECT (SELECT count(*) FROM conformed.endpoints)    AS linkable_rows,
       (SELECT count(*) FROM conformed.review_queue) AS invisible_rows,
       round(100.0 * (SELECT count(*) FROM conformed.review_queue) /
             nullif((SELECT count(*) FROM raw.design_outcomes), 0), 1) AS pct_unlinkable;

-- queued rows whose best near-miss points at a measurement already in the
-- warehouse: each one is a link that resolving the review queue would create
SELECT best_semantic_candidate AS would_join,
       count(*) AS queued_rows,
       round(max(best_semantic_score), 2) AS best_score
FROM conformed.review_queue
WHERE status = 'pending' AND best_semantic_candidate IS NOT NULL
GROUP BY 1 ORDER BY queued_rows DESC LIMIT 20;
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

## Grouping by drug class

`conformed.study_drug_class` is many-to-many with one `is_primary` row per
study, and every row carries the `kind` that says which axis the class is on.
**Filter on `kind` whenever you group**: a list that mixes them compares
"PD-1 inhibitor" against "monoclonal antibody" as if they were alternatives.

```sql
-- what is this corpus made of, by mechanism?
SELECT drug_class_id, count(DISTINCT nct_id) AS studies
FROM conformed.study_drug_class
WHERE kind = 'mechanism'
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
```

```sql
-- endpoint choice as a class-level convention: which measurements does each
-- mechanism reach for? This is the question the axis exists to answer, and
-- nothing before it could.
SELECT c.drug_class_id, e.measurement_id, count(*) AS endpoints
FROM conformed.endpoints e
JOIN conformed.study_drug_class c ON c.nct_id = e.nct_id AND c.is_primary
WHERE c.kind = 'mechanism' AND e.outcome_type = 'primary'
GROUP BY 1, 2
HAVING count(*) > 2
ORDER BY 1, 3 DESC;
```

```sql
-- has this class adopted PFS over OS, and when? The regulatory-history
-- question this corpus is uniquely shaped to answer.
SELECT c.drug_class_id, date_trunc('year', s.start_date) AS year,
       count(*) FILTER (WHERE e.measurement_id = 'progression_free_survival') AS pfs,
       count(*) FILTER (WHERE e.measurement_id = 'overall_survival') AS os
FROM conformed.endpoints e
JOIN raw.studies s USING (nct_id)
JOIN conformed.study_drug_class c ON c.nct_id = e.nct_id
WHERE e.outcome_type = 'primary' AND c.kind = 'mechanism'
GROUP BY 1, 2 HAVING pfs + os > 0 ORDER BY 1, 2;
```

```sql
-- combination therapy, straight off the many-to-many: which mechanisms are
-- studied together?
SELECT a.drug_class_id AS class_a, b.drug_class_id AS class_b,
       count(DISTINCT a.nct_id) AS studies
FROM conformed.study_drug_class a
JOIN conformed.study_drug_class b
  ON b.nct_id = a.nct_id AND b.drug_class_id > a.drug_class_id
WHERE a.kind = 'mechanism' AND b.kind = 'mechanism'
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20;
```

```sql
-- the arm tier, and how much of it you can trust. `link_method` is
-- 'join_table' (AACT's real join) or 'arm_label' (a CT.gov string match).
SELECT link_method, kind, count(*) AS rows, count(DISTINCT nct_id) AS studies
FROM conformed.arm_drug_class GROUP BY 1, 2 ORDER BY 3 DESC;
```

```sql
-- the honest denominator: how much of the corpus the axis actually covers.
SELECT
  (SELECT count(*) FROM raw.studies) AS pulled,
  (SELECT count(DISTINCT nct_id) FROM raw.interventions) AS with_interventions,
  (SELECT count(DISTINCT nct_id) FROM conformed.study_drug_class
     WHERE drug_class_id NOT IN ('unclassified_agent', 'no_interventions_stated'))
    AS classified,
  (SELECT count(*) FROM conformed.drug_class_review_queue) AS unclassified_interventions;
```

## The vocabulary tables, if you want to see the contract itself

```sql
SELECT * FROM vocab.matching_cascade ORDER BY dimension, ordinal;   -- the field cascade per dimension
SELECT * FROM vocab.matching_confidence_floor;                       -- exact/syntactic_rule/semantic floors
SELECT * FROM vocab.term_precedence WHERE dimension = 'form' ORDER BY rank;
SELECT * FROM vocab.form_disambiguation;                             -- responder vs incidence, etc.
```

## Endpoint variability

What the trials in a group actually reported, once `results conform` has run.
`endpoints stats` is the packaged version of these
([`USAGE.md`](USAGE.md#endpoint-variability)); this is what it queries, for
when you want a different cut.

**Read the caveat below first**, and the one above it: these see only rows that
conformed *and* whose trial posted results, and the intersection is narrower
than either.

**Which endpoints have enough reported dispersion to be worth asking about**

```sql
SELECT r.measurement_id, r.form_id,
       count(DISTINCT r.nct_id) AS studies,
       count(*) FILTER (WHERE d.sd_estimate IS NOT NULL) AS arms_with_sd,
       count(*) AS arms
FROM conformed.endpoint_dispersion d
JOIN conformed.endpoint_results r ON r.result_id = d.result_id
WHERE r.result_kind = 'outcome'
GROUP BY 1, 2 HAVING arms_with_sd >= 5
ORDER BY arms_with_sd DESC LIMIT 20;
```

**The SD distribution for one endpoint, pooled only within a unit**

`coalesce(si_scale_id, scale_id)` is the pooling unit -- the converted one where
`scales.yaml` declares a conversion, the reported one where it does not. Never
group on `scale_id` alone unless you want litres and millilitres in one median.

```sql
SELECT r.form_id,
       coalesce(d.si_scale_id, d.scale_id) AS unit,
       count(DISTINCT r.nct_id) AS studies,
       count(*) AS arms,
       median(coalesce(d.sd_estimate_si, d.sd_estimate)) AS sd_median,
       quantile_cont(coalesce(d.sd_estimate_si, d.sd_estimate), 0.25) AS sd_q1,
       quantile_cont(coalesce(d.sd_estimate_si, d.sd_estimate), 0.75) AS sd_q3
FROM conformed.endpoint_dispersion d
JOIN conformed.endpoint_results r ON r.result_id = d.result_id
WHERE r.measurement_id = 'fev1' AND r.result_kind = 'outcome'
  AND d.sd_estimate IS NOT NULL AND d.sd_scale = 'arithmetic'
GROUP BY 1, 2 ORDER BY arms DESC;
```

**How each SD was arrived at** -- a library built mostly out of range-derived
estimates is a different object from one built out of reported SDs

```sql
SELECT sd_method, sd_is_derived, sd_is_approximate, count(*)
FROM conformed.endpoint_dispersion WHERE sd_estimate IS NOT NULL
GROUP BY 1, 2, 3 ORDER BY 4 DESC;
```

**Where the dispersion went** -- the denominator, by reason

```sql
SELECT sd_skip_reason, count(*) FROM conformed.endpoint_dispersion
WHERE sd_estimate IS NULL GROUP BY 1 ORDER BY 2 DESC;
```

**Reported outcomes that were never registered** -- the queue, as a finding

```sql
SELECT nct_id, title_raw, measurement_id FROM conformed.results_review_queue
WHERE reason = 'unlinked_to_planned' ORDER BY nct_id LIMIT 20;
```

**Every non-inferiority margin used for one endpoint, with the trials behind it**

```sql
SELECT r.nct_id, a.param_type, a.non_inferiority_type, a.non_inferiority_description
FROM raw.outcome_analyses a
JOIN conformed.endpoint_results r
  ON r.source_id = a.outcome_id AND r.result_kind = 'outcome'
WHERE a.non_inferiority AND r.measurement_id = 'fev1';
```

(`endpoints stats --analyses` parses the margin out of that description where
it can, and shows the description where it cannot.)

**Baseline variability, which is a different quantity** -- never substitute it
for a change-score SD without saying so

```sql
SELECT r.measurement_id, coalesce(d.si_scale_id, d.scale_id) AS unit,
       count(DISTINCT r.nct_id) AS studies, median(coalesce(d.sd_estimate_si, d.sd_estimate)) AS sd
FROM conformed.endpoint_dispersion d
JOIN conformed.endpoint_results r ON r.result_id = d.result_id
WHERE r.result_kind = 'baseline' AND d.sd_estimate IS NOT NULL
GROUP BY 1, 2 ORDER BY studies DESC LIMIT 20;
```

### The caveat that goes with all of these

Three biases stack, and the aggregate is over their intersection:

1. **Not every trial posts results.** Around 70% of those under mandatory
   reporting do, and industry sponsors comply better than academic ones.
2. **Not every endpoint conforms.** Measurement coverage is 64.5% on the
   unbiased sample, head-weighted.
3. **Not every posted result carries a usable dispersion.** `sd_skip_reason`
   says why, per row.

`endpoints stats` prints the third denominator on every group; the first two
are what [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md#the-statistics-honestly)
and [`../vocab/README.md`](../vocab/README.md) are for. And `population` is not
a structured axis on either side of the warehouse, so none of these can narrow
to a per-protocol or enrichment population -- an SD pooled across a
severe-disease enrichment population and a broad one is pooled across a real
difference.
