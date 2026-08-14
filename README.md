# Clinical Study Endpoint Knowledge Base

A structured knowledge base for clinical study endpoints. It represents the internal
structure of an endpoint as a set of controlled vocabularies, connects that structure to
public registry data with a fully traceable classification pipeline, and projects the
result into CDISC USDM v4 for study exchange.

The premise is that "PFS" and "change from baseline in FEV1" are not opaque strings.
Each decomposes into a small number of orthogonal parameters — a form, a measurement, a
reference, a direction, a scale — and once you hold those explicitly you can ask
questions that free text cannot answer: which endpoints share a reference type, which
studies measured the same thing differently, which thresholds are conventions rather
than definitions.

```
┌─ Layer A ──────────────┐   ┌─ Layer B ──────────────┐   ┌─ Layer C ──────────────┐
│ Canonical concepts     │   │ Study specifications   │   │ USDM v4 projection     │
│ authored in YAML, in   │──▶│ derived from registry  │──▶│ Objective, Endpoint,   │
│ git, human-reviewable  │   │ text, fully traceable  │   │ Estimand, Timing…      │
│                        │   │                        │   │                        │
│ form · measurement ·   │   │ timepoint · threshold  │   │ SyntaxTemplate +       │
│ reference · direction  │   │ population · level     │   │ ParameterMap           │
│ · scale                │   │                        │   │                        │
└────────────────────────┘   └────────────────────────┘   └────────────────────────┘
```

---

## Status: what is and is not connected

**The vocabularies, pipeline, classifier, USDM projection, storage and UI are complete
and tested.** 82 tests pass, and every projected USDM object validates against CDISC's
own pydantic models.

**No live registry data has been ingested.** The environment this was built in blocks
`clinicaltrials.gov` at the network egress policy, along with every other clinical data
host (AACT, EMA, WHO ICTRP, NCI EVS, LOINC). Only GitHub and the package registries are
reachable. The ClinicalTrials.gov adapter is written, but it has never made a live call.

The pipeline is therefore demonstrated against a **synthetic fixture corpus**: 21 studies
and 56 outcomes whose *phrasing* imitates registry conventions but whose identifiers,
sponsors and conditions are invented. Fixture studies use `SYNTH-nnnn` identifiers rather
than NCT numbers and are badged as synthetic everywhere they appear. See
[`data/fixtures/README.md`](data/fixtures/README.md).

To connect real data, allowlist `clinicaltrials.gov` for the environment and run:

```bash
ceskb probe   --source ctgov --limit 50      # verify the API shape first
ceskb refresh --source ctgov --incremental
```

`probe` checks every declared field path against live payloads and exits non-zero if any
path fails to resolve — run it before trusting a bulk load, since the field paths were
written from documentation rather than from observed responses.

---

## Quick start

```bash
pip install -e ".[dev]"

ceskb validate                     # check vocabularies and rule packs
ceskb refresh --source fixtures    # build the whole knowledge base
ceskb stats                        # coverage and prevalence
ceskb serve                        # explore at http://127.0.0.1:8000
```

---

## The three layers

### Layer A — canonical endpoint concepts

A concept is a reusable clinical meaning that exists independently of any study. It is
authored in YAML under `vocabularies/`, reviewed in git, and validated against JSON
Schema on every load.

Structure is expressed on **16 axes**, five of which are *defining* — change one and it
is a different endpoint:

| Axis | What it captures |
|---|---|
| `endpoint_form` | The shape of the derived variable: time-to-event, responder, change from baseline, rate, slope, hierarchical composite… |
| `measurement_concept` | What is physically observed, before derivation |
| `reference_type` | What the value is compared against: own baseline, randomisation, nadir, population norm, fixed target |
| `direction` | Which way is better, including the hazard-ratio inversion for `shorter_is_better` |
| `scale_type` | The statistical scale of the analysed variable |

and eleven more that carry defaults or operational detail: `summary_measure`,
`timepoint_anchor`, `timepoint_selection`, `threshold_kind`, `threshold_operator`,
`analysis_population`, `intercurrent_event_strategy`, `endpoint_level`,
`therapeutic_area`, `measurement_modality`, `unit`.

**46 concepts** are defined across 14 therapeutic areas.

### Layer B — study specifications

What a protocol actually chose. Derived from registry text by versioned rule packs and
extractors, and — the point of the layer — every parameter records **where its value came
from**:

| Origin | Meaning |
|---|---|
| `concept_default` | Inherited from Layer A |
| `rule_assert` | A rule overrode the concept because the surface form was more specific |
| `extracted` | Read from this study's own text, with the matched span stored |
| `unresolved` | The source did not say, and nothing was invented |

### Layer C — USDM v4 projection

`Objective`, `Endpoint`, `SyntaxTemplateDictionary` with `ParameterMap`s, `Activity`,
`Timing`, `Estimand`, `AnalysisPopulation`, `IntercurrentEvent` — wrapped in the
`Study` / `StudyVersion` / `StudyDesign` envelope, targeting **USDM 4.0.0**.

Endpoint text is a syntax template carrying `[Tag]` placeholders, each resolving through
a `ParameterMap` to a `<usdm:ref>` pointing at the object that supplies the value:

```
"Progression-free survival, defined as the time from [TimeOrigin] to the first of
 documented disease progression per [Criteria] assessed by [Assessor] or death…"

  [TimeOrigin] → <usdm:tag name="TimeOrigin">randomisation</usdm:tag>
  [Criteria]   → <usdm:ref klass="Activity" id="f66b…" attribute="label"></usdm:ref>
  [Assessor]   → <usdm:ref klass="Activity" id="f66b…" attribute="label"></usdm:ref>
```

Endpoint and objective levels carry the real NCI C-codes from the CDISC DDF controlled
terminology (`C94496` Primary Endpoint, `C139173` Secondary, `C170559` Exploratory;
objectives `C85826` / `C85827` / `C163559`), extracted from
`DDF-RA/Deliverables/CT/USDM_CT.xlsx`.

---

## Traceability

The UI's trace view is the answer to "why is this endpoint classified this way". For any
specification it shows the source text with the exact spans that fired highlighted, every
axis with the origin of its value, **every rule that matched including the ones that
lost**, and every extractor hit with its matched substring and parsed value.

Nothing is inferred silently. Where the registry does not state an assessment anchor, an
analysis population, or an intercurrent event strategy, the value is `unspecified` and
says so — in the database, in the UI, and in the USDM document's provenance block.

---

## Commands

| Command | Purpose |
|---|---|
| `ceskb validate` | Validate vocabularies and rule packs. No database needed. |
| `ceskb init` | Create the database and load Layer A. |
| `ceskb probe --source ctgov` | Check declared field paths against live payloads. |
| `ceskb ingest --source {ctgov,fixtures}` | Pull studies. |
| `ceskb classify` | Derive Layer B. |
| `ceskb project` | Derive Layer C. |
| `ceskb refresh --incremental` | Ingest, classify and project in one pass. |
| `ceskb export` | Write Parquet, USDM JSON and a graph edge list. |
| `ceskb stats` | Coverage and prevalence. |
| `ceskb serve` | Run the exploration UI. |

---

## Keeping it current

`ceskb refresh --source ctgov --incremental` resumes from a stored watermark on
`LastUpdatePostDate` and transfers only what changed. Studies and outcomes are keyed by
content hash: an unchanged study has its `last_seen_at` touched and nothing rewritten,
and a changed outcome invalidates the specification derived from it.

`.github/workflows/refresh.yml` runs this weekly. It requires `clinicaltrials.gov` to be
reachable from the runner.

---

## Storage

DuckDB, in a single file. The reasoning is in
[`docs/DECISIONS.md`](docs/DECISIONS.md) — briefly: the queries this serves are joins of
bounded depth rather than variable-depth traversal, the analytical aggregations
("how often is this concept primary?") are exactly what a columnar engine is for, and a
zero-ops embedded database keeps the whole system reproducible from a git clone. For
consumers who want a graph, `ceskb export` writes a node and edge list.

---

## Layout

```
vocabularies/axes/       16 controlled vocabulary axes
vocabularies/concepts/   46 canonical endpoint concepts
rules/                   4 versioned classification rule packs, 52 rules
schemas/                 JSON Schemas enforced on every load
src/ceskb/
  vocab/                 loading, validation, referential integrity
  ingest/                sources, field-path declarations, normalisation, pipeline
  classify/              rule engine and parameter extractors
  project/               USDM v4 projection
  store/                 DuckDB schema, access, exports
  api/                   read-only HTTP API
web/                     dependency-free exploration UI
docs/                    architecture, decisions, vocabulary guide, roadmap
```

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the layers and pipeline fit together
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the choices made and why, including storage
- [`docs/VOCABULARY.md`](docs/VOCABULARY.md) — the axes, and how to extend them
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — known gaps and what comes next

## Sources

- [CDISC Digital Data Flow](https://www.cdisc.org/ddf) and [cdisc-org/DDF-RA](https://github.com/cdisc-org/DDF-RA) — USDM model, API and controlled terminology
- [cdisc-org/usdm](https://github.com/cdisc-org/usdm) — the pydantic model classes the projection validates against
- [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api)
- ICH E9(R1) Addendum on Estimands and Sensitivity Analysis in Clinical Trials

## Licence

Apache 2.0.
