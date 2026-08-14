# Decisions

Each entry records what was chosen, what it was chosen over, and what would change the
answer.

---

## 1. DuckDB as the system of record for derived data

**Chosen over:** a property graph (Neo4j), a document store (MongoDB), PostgreSQL.

The knowledge base *looks* like a graph — concepts relate to terms relate to studies —
and that intuition is what usually leads people to a graph database. It is worth
resisting here, because the queries this system actually serves are joins of **bounded
depth**:

- "which studies used this concept" — one join
- "which concepts share a reference type" — one join through `concept_structure`
- "how often is this concept primary rather than secondary" — a group-by

None of these need variable-length traversal, which is the thing a graph database is
genuinely better at. What they do need is fast aggregation over a few million rows, which
is exactly what a columnar engine is for. DuckDB also reads and writes Parquet natively,
so the bronze/silver/gold layering can move to object storage later without a rewrite,
and it is embedded, so the whole system is reproducible from a git clone with no service
to run.

The graph shape is real, so `ceskb export` writes a node and edge list for anyone who
wants to load it into a triplestore or Neo4j. That is a projection, not a second system
of record.

**What would change this:** genuine variable-depth queries — "find every path from this
measurement to any endpoint used in a registrational trial" — or a move to
multi-writer concurrent ingestion. DuckDB is single-writer. At that point Postgres with
a graph extension is the likelier answer than a dedicated graph database.

---

## 2. YAML in git as the system of record for Layer A

**Chosen over:** authoring vocabularies directly in the database.

The vocabularies are the intellectual content of this project and they need review,
history, and the ability to be argued about in a pull request. A database row has none of
those properties. The database is rebuilt from the YAML on every run
(`load_vocabulary_into_db` truncates and reloads), so the files can never drift from what
is queried.

The cost is that a vocabulary edit requires a rebuild rather than an `UPDATE`. For an
artefact that changes weekly at most and needs an audit trail, that is the right trade.

---

## 3. Rule-based classification rather than a model

**Chosen over:** embeddings plus nearest-neighbour, or an LLM classifier.

A statistical classifier would almost certainly reach higher coverage than 98% on the
first pass. It would also make "why was this endpoint classified as ORR?" unanswerable
except by gesturing at a similarity score, and this project's stated purpose is
traceability.

Every assignment here names the rule, the rule pack, the version, the field it matched
in, and the exact character span. A disputed classification can be argued from the
record and fixed by editing one regex — and the fix is reviewable, deterministic, and
provably affects only what it should.

**What would change this:** the honest answer is nothing about the primary path — the
audit trail is the product. But a model has an obvious role in the **gap queue**:
clustering the unclassified outcomes on the Gaps view to propose new rules for a human
to accept. That is model-assisted authoring, not model-based classification, and it keeps
the audit trail intact.

---

## 4. Structure stored long-form, not as wide columns

`concept_structure` and `endpoint_spec_axis` are `(entity, axis_id, term_id)` rather than
one column per axis.

Adding a seventeenth axis then requires no migration, and cross-axis questions — "the
distribution of every parameter for concepts observed in phase 3 oncology" — are one
query rather than seventeen. `endpoint_spec` also keeps a denormalised wide copy of the
handful of axes the UI filters on constantly, which is a deliberate duplication for read
speed.

---

## 5. Precedence: concept, then rule, then extractor

Fixed and documented in `classify/engine.py`:

1. **Defining axes** (form, measurement, reference, direction, scale) come from the
   concept. Only a *rule* may override them — a regex over a title is never allowed to
   redefine what an endpoint fundamentally is.
2. **Default axes** (summary measure, timepoint selection) come from the concept, and may
   be overridden by a rule and then by an extractor.
3. **Operational axes** (timepoint anchor, analysis population, thresholds) come from
   extractors, because they are protocol choices only the study text can supply.

Two consequences are load-bearing:

- A time-to-event endpoint's `timepoint_selection` is locked to `first_occurrence`,
  because it is `first_occurrence` by construction. Where a title genuinely describes a
  landmark rate ("PFS rate at 12 months"), a rule asserts `responder_binary` instead —
  and does so at low confidence, because the two are routinely conflated and the
  distinction deserves human review.
- Thresholds of kind `composite_criteria`, `category_attainment` or `event_occurrence`
  are locked to the concept. "ACR20" does not mean "20 percent of some quantity"; it
  means a seven-component criteria set, and free-text extraction cannot restate that
  faithfully. Thresholds that are *conventions* (5% weight loss, HbA1c < 7%) do yield to
  a value stated in the study's own text.

---

## 6. "Unspecified" is a value, not a gap to be filled

Registry records do not state intercurrent event strategies. They frequently do not state
an assessment anchor — "At Week 16" from *what*? — or an analysis population.

The tempting move is to default these: assume randomisation, assume ITT, assume treatment
policy. That would make the knowledge base look more complete and be quietly wrong, and
because the errors would be invisible they would propagate into every downstream
comparison.

So `unspecified` is a real term on the relevant axes, `unresolved` is a real origin, and
the USDM projection carries a provenance block naming exactly which attributes were not
determinable. In the fixture corpus 22 of 56 outcomes have no resolvable timepoint
anchor. That number is a finding about registry data, not a defect to be hidden.

---

## 7. External terminology mappings are marked unverified by default

The JSON Schema requires `verified: true` to be accompanied by `verified_against` naming
the artefact checked. Only the CDISC C-codes qualify — they were extracted from
`USDM_CT.xlsx` in the course of building this. Every UCUM code is marked `verified: false`
because no UCUM release was reachable to check them against.

A wrong code is worse than an absent one, because an absent code prompts a lookup and a
wrong one does not. The UI shows the verified/unverified badge on every mapping, so the
verification debt is visible rather than assumed away.

---

## 8. Registry outcome levels map to endpoint levels, and objectives are synthesised

ClinicalTrials.gov has `primaryOutcomes` / `secondaryOutcomes` / `otherOutcomes`, which
map cleanly onto the USDM `Endpoint.level` codelist. It has **no objectives at all**.

USDM requires endpoints to hang off objectives, so the projection synthesises one
objective per level and attaches that level's endpoints to it. This is a structural
necessity of the target model, not a claim that the sponsor wrote those objectives, and
the objective text is a template rather than invented prose. When a real protocol source
is available, its objectives should replace the synthesised ones.

---

## 9. Fixtures are synthetic and unmistakably so

The build environment blocks every clinical data host. Rather than hand-transcribing real
trials from memory — which risks fabricating trial data, the worst possible failure for a
system whose value is traceability — the fixtures use `SYNTH-nnnn` identifiers, invented
sponsors, and a `_synthetic` marker that the pipeline propagates into the database and
the UI badges on every screen.

What is realistic is only the *phrasing* of outcome measures, which is what the
classifier needs to be exercised against.
