# Architecture

## Data flow

```
  vocabularies/*.yaml          rules/*.yaml
  schemas/*.schema.json             │
          │                         │
          ▼                         ▼
  ┌──────────────────────────────────────────┐
  │ vocab.loader                             │  validates against JSON Schema,
  │  Axis · Term · Concept · Rule            │  then cross-checks every reference
  └──────────────────────────────────────────┘
          │                         │
          │ load_vocabulary_into_db │
          ▼                         │
  ┌───────────────┐                 │
  │ Layer A tables│                 │
  └───────────────┘                 │
                                    │
  ClinicalTrials.gov API v2         │
  (or local fixtures)               │
          │                         │
          ▼                         │
  ┌──────────────┐  bronze   ┌──────────────┐
  │ ingest.      │──────────▶│ data/bronze/ │  immutable hashed payloads
  │  sources     │           └──────────────┘
  └──────────────┘
          │ normalise (FIELD_PATHS declared in one place)
          ▼
  ┌──────────────────────────┐
  │ study · study_outcome    │  silver, keyed by content hash
  └──────────────────────────┘
          │                         │
          ▼                         ▼
  ┌──────────────────────────────────────────┐
  │ classify.engine                          │
  │  rules → concept, extractors → params    │
  │  precedence: concept ▸ rule ▸ extractor  │
  └──────────────────────────────────────────┘
          │
          ├──▶ endpoint_spec, endpoint_spec_axis          (Layer B)
          ├──▶ classification_evidence, extraction_evidence (provenance)
          └──▶ unclassified_outcome                        (coverage denominator)
          │
          ▼
  ┌──────────────────────────┐
  │ project.usdm             │  USDM v4.0.0 documents
  └──────────────────────────┘
          │
          ├──▶ usdm_projection      (Layer C)
          └──▶ api → web UI, store.export → Parquet / USDM JSON / graph
```

## Modules

| Module | Responsibility |
|---|---|
| `ceskb.vocab.loader` | Loads and validates axes, concepts and rule packs; enforces referential integrity across all of them. The only way anything reads vocabulary. |
| `ceskb.ingest.sources` | `CtgovApiSource` (paginated, rate-limited, retrying) and `FixtureSource`. Both yield records in CT.gov API v2 shape. |
| `ceskb.ingest.normalise` | `FIELD_PATHS` declares every source field path in one place. `probe_schema` reports which resolved, so upstream drift surfaces as a number. |
| `ceskb.ingest.pipeline` | Bronze snapshots, content-hash upserts, watermarks, run recording. |
| `ceskb.classify.extractors` | Timepoint anchor/offset/selection, threshold, analysis population. Each returns a value **and** the span that justified it. |
| `ceskb.classify.engine` | Applies rules, resolves competition, layers structure by precedence, applies reviewer overrides, writes evidence. |
| `ceskb.review.overrides` | Reviewer decisions: YAML in git as the system of record, keyed by outcome so they survive re-derivation, hash-bound so they go stale when the source text changes. |
| `ceskb.evaluate.gold` | Gold-set loading and validation, and Cohen's kappa between two annotators. |
| `ceskb.evaluate.score` | Per-concept precision/recall, axis accuracy, difficulty bands, CI gates. |
| `ceskb.project.usdm` | Builds USDM v4 objects with real NCI C-codes. |
| `ceskb.store.db` | Schema, connection, Layer A rebuild, watermarks, run tracking. |
| `ceskb.store.export` | Parquet, USDM JSON, graph edge list. |
| `ceskb.api.app` | FastAPI over the DuckDB file, read-only except for the opt-in review write endpoint. |

## Incremental refresh

`ceskb refresh --incremental` is the operation a scheduler calls.

1. Read the `last_update_posted` watermark for the source.
2. Query with `filter.advanced=AREA[LastUpdatePostDate]RANGE[<watermark>,MAX]`.
3. For each study, compute a content hash over the fields that matter. Unchanged →
   touch `last_seen_at` only. Changed → replace the study row, and for each outcome whose
   own hash changed, delete the derived `endpoint_spec` so it is re-derived.
4. Reclassify and re-project.
5. Advance the watermark to the highest `lastUpdatePostDate` seen.

Classification is idempotent and deterministic: `spec_id` is a UUID5 over
`(outcome_uid, derivation_version)`, so the same input always produces the same
identifier, and bumping `DERIVATION_VERSION` in `config.py` invalidates every derived row
at once.

## Adding a second registry

Write an adapter that yields records in CT.gov API v2 shape and register it in
`build_source`. Nothing downstream needs to know. If a registry's native shape differs
enough that translating is awkward, the cleaner path is a second `FIELD_PATHS` mapping
selected per source — the normaliser is the only module that reads source structure.

## Extending the model

- **A new concept:** add it to a file in `vocabularies/concepts/` and a rule to
  `rules/`. `ceskb validate` fails on any unresolved reference.
- **A new axis:** add `vocabularies/axes/<axis>.yaml`. If concepts should carry it, add
  it to `STRUCTURE_AXES` in `vocab/loader.py` with a `defining` or `default` role. No
  database migration is needed — structure is stored long-form.
- **A new extractor:** add it to `classify/extractors.py` and include it in
  `SINGLE_EXTRACTORS` or `run_all`. It must return an `Extraction` carrying the matched
  span, or it will not be accepted by the evidence tables.
