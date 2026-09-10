# Entity-relationship diagram: the `conformed` layer

> **Status: reference.** Drawn from the DDL in `conform/pipeline.py`,
> `results/pipeline.py`, `ta/resolver.py` and `drug_class/resolver.py` — those
> modules are the source of truth, this document is a picture of them. Where a
> column here disagrees with a `CREATE OR REPLACE TABLE` there, the DDL wins and
> this file is stale. The attribute blocks below carry each table's keys, its
> grain columns and every column that participates in a relationship; the full
> column lists stay in the DDL rather than being copied here, so there is one
> place to change when a column moves.

Read alongside:

* [`USAGE.md`](USAGE.md), "The warehouse" — the three schemas, and which command writes each
* [`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md) — the SQL that walks these edges
* [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) — why the planned and reported halves share a column block
* [`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md) — why class is resolved at both study and arm grain

---

## Read this before the diagrams

**There is not one foreign key in this warehouse.** DuckDB is given
`PRIMARY KEY` on five of the nine tables and nothing else. Every edge drawn
below is a *convention the pipeline maintains*, not a constraint the database
enforces — so a dangling reference is possible in principle, and one case
(see [Five edges that are not what they look like](#five-edges-that-are-not-what-they-look-like))
is deliberate.

**Every `conformed.*` table is replaced wholesale, never appended to.** Each
writer opens with `CREATE OR REPLACE TABLE`. These tables are derived from
`raw.*` and `vocab.*` and hold no state worth preserving independently of their
sources — which is also why the review queues carry a `status` column that no
command can currently write back (`endpoints review resolve` is declared and
unimplemented; see [`README.md`](README.md)).

**Ids are content hashes, not surrogates.** `endpoint_id`, `review_id`,
`result_id` and `dispersion_id` are each `md5` over the row's own identifying
text, so re-running a command over unchanged input reproduces the same ids.
Two consequences no diagram can draw: byte-identical source rows *collapse onto
one id* rather than duplicating, and ids are comparable across two warehouses
built from the same registry snapshot.

---

## The layer at a glance

Nine tables, written by three commands. Boundary entities from `raw.*` are drawn
without attributes — they are where the layer's rows come from, not part of it.

```mermaid
erDiagram
    direction LR

    raw_studies                ||--o{ raw_design_outcomes       : "plans"
    raw_studies                ||--o{ raw_outcome_measures      : "reported"
    raw_studies                ||--o{ raw_baseline_measurements : "reported"
    raw_studies                ||--o{ raw_interventions         : "tested"

    raw_design_outcomes        ||--o| endpoints                 : "conformed into"
    raw_design_outcomes        ||--o| review_queue              : "or queued as"
    raw_outcome_measures       ||--o| endpoint_results          : "result_kind='outcome'"
    raw_baseline_measurements  ||--o| endpoint_results          : "result_kind='baseline'"
    raw_interventions          ||--o{ drug_class_review_queue   : "queued unclassified"

    raw_studies                ||--o{ study_therapeutic_area    : "classified into"
    raw_studies                ||--o{ study_drug_class          : "classified into"
    raw_studies                ||--o{ arm_drug_class            : "arms classified into"

    endpoints                  ||--o{ endpoint_results          : "planned_endpoint_id"
    endpoint_results           ||--o{ endpoint_dispersion       : "result_id"
    endpoint_results           ||--o| results_review_queue      : "review_id = result_id"
```

## The planned side

One `raw.design_outcomes` row conforms into `endpoints` **or** is queued in
`review_queue` — never both, never neither. The two tables share an id space:
a queued row's `review_id` is the `endpoint_id` it would have had.

`event_id` is nullable by design, and its NULL carries meaning. Only
event-family forms (`vocab.forms.event_family`) get an event at all, so a
change-from-baseline endpoint has `event_id IS NULL` because it *has no event*,
not because one went unresolved. That is the opposite of the axes whose
matching cascade ends in a `not_stated` fallback term (`vocab/matching.yaml`) —
there, a miss is a value you can group by, not a NULL.

```mermaid
erDiagram
    direction LR

    raw_design_outcomes ||--o| endpoints    : "conformed into"
    raw_design_outcomes ||--o| review_queue : "or queued as"

    endpoints {
        VARCHAR   endpoint_id        PK "md5(nct_id|outcome_type|measure|time_frame|description)"
        VARCHAR   nct_id             FK "raw.studies"
        VARCHAR   outcome_type          "primary/secondary/other, verbatim -- case varies by backend"
        VARCHAR   measure_raw           "registry text, verbatim"
        VARCHAR   description_raw
        VARCHAR   time_frame_raw
        VARCHAR   population
        VARCHAR   form_id            FK "vocab.forms"
        VARCHAR   measurement_id     FK "vocab.measurements"
        VARCHAR   reference_id       FK "vocab.references"
        VARCHAR   event_id           FK "vocab.events, nullable"
        VARCHAR   named_endpoint_id  FK "vocab.named_endpoints, nullable"
        VARCHAR   direction_id       FK "vocab.directions"
        VARCHAR   scale_id           FK "vocab.scales"
        VARCHAR   timepoint_pattern  FK "vocab.timepoint_patterns"
        VARCHAR   match_provenance      "per axis: _match_method, _confidence, _source_field"
        VARCHAR   event_polarity_used
        VARCHAR   timepoint_raw
        JSON      timepoint_extracted
        VARCHAR   threshold_comparator
        DOUBLE    threshold_value
        VARCHAR   threshold_unit
        BOOLEAN   analysable
        VARCHAR   usdm_text             "rendered USDM syntax template"
        TIMESTAMP conformed_at
    }

    review_queue {
        VARCHAR   review_id              PK "the endpoint_id this row would have had"
        VARCHAR   nct_id                 FK "raw.studies"
        VARCHAR   outcome_type
        VARCHAR   measure_raw
        VARCHAR   description_raw
        VARCHAR   time_frame_raw
        VARCHAR   population
        VARCHAR   reason                    "measurement_unmatched"
        VARCHAR   candidate_form_id      FK "vocab.forms, nullable"
        VARCHAR   candidate_direction_id FK "vocab.directions, nullable"
        VARCHAR   best_semantic_candidate
        DOUBLE    best_semantic_score
        VARCHAR   status                    "pending; nothing resolves it yet"
        TIMESTAMP queued_at
    }
```

## The reported side

`endpoint_results` carries the same dimension columns as `endpoints`, under the
same names — `tests/test_results_conform.py` asserts it — so a query written
against the planned half runs unchanged against the reported half.

```mermaid
erDiagram
    direction LR

    raw_outcome_measures      ||--o| endpoint_results : "result_kind='outcome'"
    raw_baseline_measurements ||--o| endpoint_results : "result_kind='baseline'"
    endpoints                 ||--o{ endpoint_results : "planned_endpoint_id"
    endpoint_results          ||--o{ endpoint_dispersion    : "result_id"
    endpoint_results          ||--o| results_review_queue   : "review_id = result_id"

    endpoint_results {
        VARCHAR   result_id           PK "md5(result_kind|source_id)"
        VARCHAR   result_kind            "outcome | baseline"
        VARCHAR   source_id           FK "outcome_measures.outcome_id OR baseline_measurements.baseline_id"
        VARCHAR   nct_id              FK "raw.studies"
        VARCHAR   measure_raw            "the REPORTED title, not the planned measure"
        VARCHAR   dimension_block        "form_id, measurement_id, ... : as conformed.endpoints"
        VARCHAR   link_method            "exact_title | conformed_measurement | NULL"
        VARCHAR   planned_endpoint_id FK "endpoints, nullable"
        BOOLEAN   link_agrees_on_form    "NULL when no planned row is pointed at"
        TIMESTAMP conformed_at
    }

    results_review_queue {
        VARCHAR   review_id     PK "equals endpoint_results.result_id"
        VARCHAR   result_kind
        VARCHAR   source_id
        VARCHAR   nct_id        FK "raw.studies"
        VARCHAR   outcome_type
        VARCHAR   title_raw
        VARCHAR   description_raw
        VARCHAR   time_frame_raw
        VARCHAR   reason           "measurement_unmatched | unlinked_to_planned"
        VARCHAR   measurement_id FK "vocab.measurements, nullable"
        VARCHAR   best_semantic_candidate
        DOUBLE    best_semantic_score
        VARCHAR   status
        TIMESTAMP queued_at
    }

    endpoint_dispersion {
        VARCHAR   dispersion_id     PK "md5(result_id|group_key|class_title|category_title)"
        VARCHAR   result_id         FK "endpoint_results"
        VARCHAR   result_kind
        VARCHAR   source_id
        VARCHAR   nct_id            FK "raw.studies"
        VARCHAR   group_key            "the arm, as the RESULTS section keys it"
        VARCHAR   group_title
        VARCHAR   class_title
        VARCHAR   category_title
        VARCHAR   param_type_raw       "verbatim; param_kind is the normalisation"
        VARCHAR   dispersion_type_raw  "verbatim; dispersion_kind is the normalisation"
        DOUBLE    confidence_percent   "read from the string, never assumed 95"
        VARCHAR   unit_raw
        VARCHAR   scale_id          FK "vocab.scales"
        INTEGER   n
        VARCHAR   n_source
        DOUBLE    central_value
        DOUBLE    sd_estimate
        VARCHAR   sd_method            "which conversion produced it"
        BOOLEAN   sd_is_derived
        BOOLEAN   sd_is_approximate
        VARCHAR   sd_scale             "arithmetic | log"
        VARCHAR   sd_skip_reason       "why there is no estimate"
        JSON      sd_inputs
        DOUBLE    sd_estimate_si       "NULL unless sd_scale is arithmetic"
        VARCHAR   si_scale_id       FK "vocab.scales"
        TIMESTAMP computed_at
    }
```

## The study axes

Both axes are resolved by `pull` itself, from what that one pull already
fetched — there is no second network wave for either, and no separate resolve
command: `endpoints ta` and `endpoints drug-class` only *report* on what `pull`
wrote. None of these four tables declares a key.

```mermaid
erDiagram
    direction LR

    raw_studies       ||--o{ study_therapeutic_area  : "classified into"
    raw_studies       ||--o{ study_drug_class        : "classified into"
    raw_studies       ||--o{ arm_drug_class          : "arms classified into"
    raw_interventions ||--o{ drug_class_review_queue : "queued unclassified"

    study_therapeutic_area {
        VARCHAR nct_id     FK "raw.studies -- part of the grain"
        VARCHAR ta_id      FK "vocab.therapeutic_areas -- part of the grain"
        VARCHAR rule_layer    "which mapping layer matched"
        VARCHAR matched_on    "the MeSH term or pattern that matched"
        BOOLEAN is_primary    "exactly one true row per study"
    }

    study_drug_class {
        VARCHAR nct_id        FK "raw.studies -- part of the grain"
        VARCHAR drug_class_id FK "vocab.drug_classes -- part of the grain"
        VARCHAR kind             "pharmacologic | control | ..."
        VARCHAR rule_layer
        VARCHAR matched_on
        BOOLEAN is_primary       "exactly one true row per study"
    }

    arm_drug_class {
        VARCHAR nct_id        FK "raw.studies"
        VARCHAR group_title      "the arm, as the PROTOCOL titles it"
        VARCHAR drug_class_id FK "vocab.drug_classes"
        VARCHAR kind
        VARCHAR rule_layer
        VARCHAR matched_on
        VARCHAR link_method      "how the intervention was tied to the arm"
    }

    drug_class_review_queue {
        VARCHAR nct_id  FK "raw.studies"
        INTEGER ordinal    "raw.interventions.ordinal within the study"
        VARCHAR name       "the intervention name that matched nothing"
        VARCHAR mesh_term
        VARCHAR reason     "unclassified_agent"
    }
```

## Grain and key, table by table

| table | grain — one row per | key | written by |
|---|---|---|---|
| `endpoints` | planned outcome that conformed | `endpoint_id` (PK) | `conform` |
| `review_queue` | planned outcome that did **not** conform | `review_id` (PK) | `conform` |
| `endpoint_results` | reported outcome, or baseline characteristic | `result_id` (PK) | `results conform` |
| `results_review_queue` | reported outcome needing review | `review_id` (PK) | `results conform` |
| `endpoint_dispersion` | arm × class × category measurement | `dispersion_id` (PK) | `results conform` |
| `study_therapeutic_area` | (study, therapeutic area) | `(nct_id, ta_id)`, undeclared | `pull` |
| `study_drug_class` | (study, drug class) | `(nct_id, drug_class_id)`, undeclared | `pull` |
| `arm_drug_class` | (study, arm, drug class) per contributing intervention | none — see below | `pull` |
| `drug_class_review_queue` | intervention that matched no class | `(nct_id, ordinal)`, undeclared | `pull` |

`arm_drug_class` is the one table without a key even by convention. An arm
holding two interventions that resolve to the *same* class contributes two
rows, differing only in `rule_layer`/`matched_on`. Count arms with
`count(DISTINCT (nct_id, group_title))`, never `count(*)`.

Note also that `endpoint_results` is at the *characteristic* grain for
baselines, not the arm grain: one FEV1 baseline characteristic conforms once
however many arms reported it, and the per-arm detail is `endpoint_dispersion`.

## The vocabulary joins

Every vocabulary dimension keys on `id`, and every `*_id` column below points at
it. Because the two fact tables share their dimension block, **each edge drawn
into `endpoints` exists identically into `endpoint_results`** — drawn once here
rather than sixteen times.

```mermaid
erDiagram
    direction LR

    vocab_forms              ||--o{ endpoints : "form_id"
    vocab_measurements       ||--o{ endpoints : "measurement_id"
    vocab_references         ||--o{ endpoints : "reference_id"
    vocab_events             ||--o{ endpoints : "event_id"
    vocab_directions         ||--o{ endpoints : "direction_id"
    vocab_scales             ||--o{ endpoints : "scale_id"
    vocab_timepoint_patterns ||--o{ endpoints : "timepoint_pattern"
    vocab_named_endpoints    ||--o{ endpoints : "named_endpoint_id"

    vocab_forms              ||--o{ review_queue         : "candidate_form_id"
    vocab_directions         ||--o{ review_queue         : "candidate_direction_id"
    vocab_measurements       ||--o{ results_review_queue : "measurement_id"
    vocab_scales             ||--o{ endpoint_dispersion  : "scale_id, si_scale_id"

    vocab_therapeutic_areas  ||--o{ study_therapeutic_area : "ta_id"
    vocab_drug_classes       ||--o{ study_drug_class       : "drug_class_id"
    vocab_drug_classes       ||--o{ arm_drug_class         : "drug_class_id"
```

## Inside the vocabulary

Two structures in `vocab.*` are worth drawing, because a query that walks the
conformed layer will eventually walk them too.

```mermaid
erDiagram
    direction LR

    vocab_named_endpoints }o--|| vocab_forms        : "form_id"
    vocab_named_endpoints }o--o| vocab_events       : "event_id"
    vocab_named_endpoints }o--o| vocab_references   : "reference_id"
    vocab_named_endpoints }o--o| vocab_measurements : "default_measurement_id"

    vocab_drug_classes    ||--o{ vocab_drug_classes : "parent"
```

`vocab.named_endpoints` is the only vocabulary table that reaches several
dimensions at once: recognising "PFS" can settle measurement, reference, form
and event together, rather than donating the name to one dimension as a
synonym. It is a *fallback*, not an override — each of those dimensions runs
its own ordinary cascade first, and the named-endpoint value fills only where
that cascade came back silent (the reference also requires a randomised
allocation). So `named_endpoint_id` and the dimension ids beside it are not a
parent and its expansion: they agree on rows the cascade could not resolve
alone, and the `*_match_method` column reading `named_endpoint` is what says
which axis was filled that way.

`vocab.drug_classes.parent` is the only self-reference anywhere in the
warehouse — one shallow level of rollup, validated at load time as acyclic and
as never changing `kind` halfway up (a mechanism term must not roll up into a
modality one). The resolver does **not** walk it: primary class is chosen by
the flat `precedence` column, tie-broken by how many interventions back each
class. So `parent` is available to a query that wants to roll up, and is not
already applied to what the conformed tables hold.

## Five edges that are not what they look like

**`endpoint_results.link_method` can be non-NULL while `planned_endpoint_id`
is NULL.** That is intentional, not a dangling reference. It means the reported
title matched a planned outcome verbatim, but that planned outcome went to
`review_queue` rather than `endpoints` — the link is real, the target is not
there to point at. A query counting linked results has to test the column it
actually means.

**`results_review_queue` overlaps `endpoint_results`; it does not partition
it.** The planned side is a clean split — a `raw.design_outcomes` row lands in
`endpoints` or in `review_queue`, never both. The reported side is not. A row
queued `measurement_unmatched` never reached `endpoint_results`; a row queued
`unlinked_to_planned` is *also* in `endpoint_results`, fully conformed, with
`link_method` NULL. `WHERE reason = ...` is what separates them.

**`endpoint_dispersion.group_key` and `arm_drug_class.group_title` are not the
same arm.** `group_key` is the results section's own arm id; `group_title` is
the protocol's arm title. How often the two agree has not been measured, which
is why `endpoints stats` does not group dispersion by drug class (see
`write_arm_drug_class`'s docstring, and [`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md),
"Class is an arm property"). Joining them yields a number resting on an
unmeasured join.

**`outcome_type` is not case-normalised anywhere.** It is stored as the backend
wrote it, on `endpoints`, `review_queue`, `endpoint_results` and
`results_review_queue` alike — the CT.gov API path writes `primary`, AACT
writes `Primary`. `WHERE outcome_type = 'PRIMARY'` silently returns nothing on
either. Compare case-insensitively. (`raw.outcome_measures`'s `outcome_id` hash
*does* case-fold it, so re-pulling a study through the other backend does not
renumber its results rows — but the stored column is still verbatim.)

**`endpoint_results.measure_raw` holds the reported title, not the planned
`measure`.** The name is shared with `conformed.endpoints` deliberately: a query
that has to be rewritten to move between the planned and reported halves is a
query that will silently be wrong on one of them.

## Reaching back into `raw`

The layer keeps enough of a handle to get back to its source rows, which is how
the arithmetic behind a number gets audited:

| from | to | on |
|---|---|---|
| `endpoint_results` (`result_kind='outcome'`) | `raw.outcome_measures` | `source_id = outcome_id` |
| `endpoint_results` (`result_kind='outcome'`) | `raw.outcome_analyses` | `source_id = outcome_id` |
| `endpoint_results` (`result_kind='baseline'`) | `raw.baseline_measurements` | `source_id = baseline_id` |
| `endpoint_dispersion` | `raw.outcome_measurements` | `(source_id, group_key, class_title, category_title)` |
| `endpoint_dispersion` | `raw.outcome_groups` | `(source_id, group_key)` |
| `arm_drug_class` | `raw.arm_interventions` | `(nct_id, group_title)` |
| `drug_class_review_queue` | `raw.interventions` | `(nct_id, ordinal)` |
| any table | `raw.studies` | `nct_id` |

`source_id` is polymorphic: it means something only together with `result_kind`,
because an `outcome_id` and a `baseline_id` are drawn from different id spaces.
Every join above that uses it must carry the `result_kind` predicate too.
