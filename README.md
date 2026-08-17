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
             ▲                            │
             │      ┌─────────────────────▼──────────────────────┐
             └──────│ Review: gold-standard scoring, queue,      │
                    │ reviewer overrides that outlive rule edits │
                    └────────────────────────────────────────────┘
```

---

## Status: what is and is not connected

**The vocabularies, pipeline, classifier, USDM projection, storage, review loop and UI
are complete and tested.** 126 tests pass, and every projected USDM object validates
against CDISC's own pydantic models.

**No live registry data has been ingested.** The environment this was built in blocks
`clinicaltrials.gov` at the network egress policy, along with every other clinical data
host (AACT, EMA, WHO ICTRP, NCI EVS, LOINC). Only GitHub and the package registries are
reachable. The ClinicalTrials.gov adapter is written, but it has never made a live call.

The pipeline is therefore demonstrated against a **synthetic fixture corpus**: 21 studies
and 56 outcomes whose *phrasing* imitates registry conventions but whose identifiers,
sponsors and conditions are invented. Fixture studies use `SYNTH-nnnn` identifiers rather
than NCT numbers and are badged as synthetic everywhere they appear. See
[`data/fixtures/README.md`](data/fixtures/README.md).

Re-checked 2026-08-14: the gateway still answers `403 to CONNECT — policy denial` for
`clinicaltrials.gov:443`. Only the environment's network policy can change this; it is
unrelated to the operator's own network, since the container runs in the cloud.

To connect real data, allowlist `clinicaltrials.gov` for the environment and run:

```bash
ceskb probe   --preset phase3-recent-100 --limit 50   # verify the API shape first
ceskb refresh --preset phase3-recent-100
```

`probe` checks every declared field path against live payloads and exits non-zero if any
path fails to resolve — run it before trusting a bulk load, since the field paths were
written from documentation rather than from observed responses.

### Scope presets

| Preset | Selects |
|---|---|
| `phase3-recent-100` | The 100 most recently updated phase 3 interventional studies. The testing slice. |
| `phase3-recent-1000` | The same, at a size where coverage numbers start to mean something. |
| `phase2-3-oncology` | 500 recent phase 2/3 oncology studies — the densest area of the concept set. |

A preset supplies defaults only; any explicit flag wins, so
`--preset phase3-recent-100 --max-studies 25` pulls 25. Presets compile to an Essie
expression, e.g. `AREA[Phase]PHASE3 AND AREA[StudyType]INTERVENTIONAL`, combined with
the incremental date range when `--incremental` is set.

**Capped runs deliberately do not advance the update watermark.** A run sorted by
recency and capped at 100 has seen only the head of the result set; advancing the
watermark would make the next incremental run start after records it never fetched.
`ceskb ingest` reports `truncated` and `watermark_advanced` so this is visible.

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

## Conforming free text to the vocabulary

Registry outcomes are free text; the vocabulary is structured. Two mechanisms bridge
them, and most of the work is done by neither.

**Rule packs identify the concept.** Versioned regexes over `measure` and `description`
map text to one of 46 concept ids — classification over a closed set, not open-ended
parsing.

**Extractors read the operational parameters** — timepoint anchor and offset, threshold
kind, operator and value, analysis population — each recording the character span that
justified it.

**Everything else is inherited.** Once the concept is identified, its form, measurement,
reference, direction and scale come from Layer A, asserted once by a human at authoring
time from clinical knowledge. Across the fixture corpus:

| Origin | Share of resolved structure |
|---|---|
| `concept_default` — inherited from Layer A | **69%** |
| `extracted` — read from this study's text | 16% |
| `unresolved` — the source was silent | 13% |
| `rule_assert` — a rule overrode the concept | 2% |

Nobody tries to read `reference_type` out of *"Percentage of Participants Achieving at
Least 10% Reduction in Body Weight"*. It is not in there. It is in
`WEIGHT_LOSS_RESPONDER`. The free text only has to answer *which concept*, plus a handful
of operational questions — which is what makes the problem tractable, and why extractors
are structurally forbidden from touching the five defining axes.

### Measuring whether it is right

Coverage says how many outcomes matched something. **Accuracy** says how many matched the
right thing, and only annotation can tell you that:

```bash
ceskb evaluate --min-precision 0.95 --max-excluded 0
```

Gold sets live in `review/gold/`, and `docs/ANNOTATION.md` is the guideline for producing
one. The harness scores per-concept precision and recall, axis-level accuracy, and
difficulty bands; gates CI per concept rather than on the average; and reports the
denominator it used.

Three properties keep the number honest:

- **`independence` is required.** The shipped set is `self_annotated` — the same party
  wrote the rules and the answers — so it detects regressions and proves nothing about
  accuracy. The scorer prints that caveat rather than letting the figure travel alone.
  Run `ceskb agreement a.yaml b.yaml` on two independent annotations *first*: if two
  people disagree, the vocabulary is underspecified and no classifier work will fix it.
- **Annotations are bound to their text by hash** and excluded from scoring if it
  changes, rather than graded against words the annotator never read.
- **Precision is gated, recall is allowed to lag.** An unmatched outcome sits visibly in
  the Gaps view; a wrongly matched one silently joins a prevalence count and a USDM
  document.

Building this immediately found three real defects that 98% coverage had not — SGRQ's
inverted direction, a missing form assertion on percent-predicted FEV1, and an extractor
overwriting a concept's own threshold definition with a vaguer reading of the same number.

### Review and overrides

`ceskb review` lists specifications with a reason to doubt them — an arbitrary tie-break,
a contested match, low confidence, or several unresolved parameters — ranked by how much
doubt there is. The queue is a view, so re-classifying keeps it correct, and anything
decided drops out.

```bash
ceskb override NCT01234567:primary:0 --concept ORR \
    --reason "Protocol §9.2: best overall response of confirmed CR or PR." \
    --reviewer "your name"
```

Decisions are written to `review/overrides.yaml` — in git, diffable, reviewable in a pull
request — and are **keyed by outcome, not by specification**, so they survive a rule
change or a `DERIVATION_VERSION` bump. Each records the outcome's content hash, and goes
*stale* if the registry rewrites the text underneath it: a judgement about words that no
longer exist stops being applied rather than silently carrying over.

An override is the one thing permitted to change a defining axis. A regex over a title
must not redefine what an endpoint is; a person who has read the protocol may.

The Review view in the UI shows all of this, and can record decisions when the server is
started with `ceskb serve --allow-review`. It is off by default — the API is otherwise
strictly read-only.

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
| `ceskb evaluate` | Score classifications against a gold set, and gate on the result. |
| `ceskb agreement A B` | Inter-annotator agreement between two gold sets. |
| `ceskb review` | List specifications awaiting human review. |
| `ceskb override` | Record a reviewer decision. |
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
review/gold/             gold-standard annotations, for measuring accuracy
review/overrides.yaml    reviewer decisions, keyed by outcome
schemas/                 JSON Schemas enforced on every load
src/ceskb/
  vocab/                 loading, validation, referential integrity
  ingest/                sources, field-path declarations, normalisation, pipeline
  classify/              rule engine and parameter extractors
  evaluate/              gold sets, scoring, inter-annotator agreement
  review/                overrides that survive re-derivation
  project/               USDM v4 projection
  store/                 DuckDB schema, access, exports
  api/                   HTTP API, read-only unless review writes are enabled
web/                     dependency-free exploration UI
docs/                    architecture, decisions, vocabulary, annotation, roadmap
```

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the layers and pipeline fit together
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the choices made and why, including storage
- [`docs/VOCABULARY.md`](docs/VOCABULARY.md) — the axes, and how to extend them
- [`docs/ANNOTATION.md`](docs/ANNOTATION.md) — how to produce a gold standard, and how it is scored
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — known gaps and what comes next

## Sources

- [CDISC Digital Data Flow](https://www.cdisc.org/ddf) and [cdisc-org/DDF-RA](https://github.com/cdisc-org/DDF-RA) — USDM model, API and controlled terminology
- [cdisc-org/usdm](https://github.com/cdisc-org/usdm) — the pydantic model classes the projection validates against
- [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api)
- ICH E9(R1) Addendum on Estimands and Sensitivity Analysis in Clinical Trials

## Licence

Apache 2.0.
