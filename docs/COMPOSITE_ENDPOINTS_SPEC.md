# Design spec: composite endpoint decomposition

Status: **proposed, not scheduled.** Written after the build-order step 4 review
(see "Why this and not the graph projection" below). Nothing here is
implemented; this is a spec to decide against or schedule, not a plan of record.

Read alongside:

* the implementation plan §6 (the graph layer this replaces)
* `vocab/README.md` — the vocabulary schema and the judgment calls behind it
* `docs/QUERY_CHEATSHEET.md`, "Cross-study comparability" — the flat relations
  that already answer the plan's other motivating questions

---

## Why this and not the graph projection

Implementation plan §6 proposes `graph.nodes` / `graph.edges` with edge types
`HAS_ENDPOINT`, `USES_FORM`, `MEASURES`, `HAS_REFERENCE`, `IN_TA`. Every one of
those is a foreign key that already exists as a typed column on
`conformed.endpoints`. Re-encoding them as generic edge rows makes no new
question answerable, and it costs:

* **Supernodes.** 7 directions, 16 references, 18 forms against roughly 8,700
  conformed endpoints. Two hops through `direction` reaches an eighth of the
  corpus; through `form`, ~480 endpoints on average and ~2,700 for `not_stated`
  (31% of rows name a measurement and no form). Untargeted traversal degenerates
  into a table scan.
* **Quadratic materialisation.** `SAME_MEASUREMENT_DIFFERENT_FORM` as a stored
  endpoint↔endpoint edge is a self-join inside each measurement group — order
  10⁵ edges even under a uniform distribution, far worse under the real
  power-law skew, encoding what a `GROUP BY measurement_id` expresses in ~165
  rows.
* **Type loss.** `WHERE threshold_value >= 30` becomes
  `WHERE CAST(json_extract_string(properties,'$.threshold_value') AS DOUBLE) >= 30`.

Every path in the star schema is length ≤ 3 and statically known — no cycles, no
variable depth. That is the test a graph layer has to pass, and §6 fails it.

**The composite→component relation passes it.** A composite endpoint contains
component endpoints, a component can itself be a composite, and the depth is not
known at query time. It is the one structure in this domain a star schema
handles badly, and it is currently not modelled at all: `measurements.yaml`
carries `composite: true` on five terms with no components anywhere.

It is also a *small* graph — hundreds of edges over a few dozen variants, not
10⁵ over 8,700 endpoints — so the traversal machinery is proportionate to what
it buys.

## Composites are three different things

This is the modelling decision everything else depends on, and `measurements.yaml`
already half-encodes it in `method`. Folding these into one "composite" flag
would produce queries that are arithmetically fine and clinically wrong.

| kind | `method` today | semantics | terms today |
|---|---|---|---|
| **event union** | `adjudicated_composite_event` | endpoint fires if **any** component event occurs | `major_adverse_cardiovascular_event`, `perinatal_composite_outcome` |
| **scored index** | `composite_index` | components feed a **formula** with weights and internal thresholds | `acr_response_composite`, `das28`, `asas_response` |
| **criteria panel** | `laboratory_composite`, `composite_assessment` | **all** (or *n* of *m*) criteria must be met | `haematologic_response`, `elf_score`, `nutritional_status` |

The distinction decides which queries are sound:

* **Event unions are comparable by component overlap.** A 3-point MACE and a
  5-point MACE differ by two components, and that difference is the single
  largest comparability trap in cardiovascular trials — an effect on 5-point
  MACE can be driven entirely by revascularisation, a component the 3-point
  version does not contain.
* **Scored indices are not.** DAS28-CRP and DAS28-ESR share every component and
  are still different scores; component overlap says "identical" and the answer
  is no. For these, decomposition is documentation — useful for reading an
  endpoint, invalid as a comparability metric.
* **Criteria panels sit between**: overlap is meaningful, but so is the
  `required` / *n*-of-*m* rule, so the component set alone underdetermines them.

Any comparability query must therefore filter on `kind`, and the schema below
makes `kind` mandatory so the filter is possible.

## What this makes answerable

None of these are answerable today, at any coverage.

1. **"Which trials' primary endpoint includes all-cause mortality as a
   component?"** — regardless of whether the title says so. Today a MACE
   endpoint's mortality component is invisible; the endpoint reads as one
   opaque `major_adverse_cardiovascular_event`.
2. **"Are these two trials' composites comparable?"** — leaf-component overlap
   between two event unions, with the asymmetric difference spelled out (what's
   in A and not B). This is the query a meta-analyst actually needs.
3. **"3-point vs 4-point vs 5-point MACE"** — today all three collapse to one
   measurement id, so the warehouse asserts they are the same endpoint. They are
   not.
4. **"Which composites transitively contain component X, and at what depth?"** —
   the genuinely recursive one.
5. **"Which components recur across composites?"** — `myocardial_infarction`
   appearing in every CV composite is a fact about how the field builds
   endpoints, and it falls out of the same table.

## Where recursion actually arises

Being honest about this, because it is the whole justification: most composites
are depth 1. The real depth comes from a small set of nesting patterns, and it
is enough.

* **MACE laddering.** 4-point MACE is 3-point plus heart-failure
  hospitalisation; 5-point is 4-point plus coronary revascularisation. Defining
  each variant against the one below it rather than restating leaf lists is both
  how the literature describes them and how you avoid four hand-maintained lists
  drifting apart.
* **Net clinical benefit** = an efficacy composite + a safety composite. A
  composite whose components are composites, by construction.
* **ACR response** contains "joint count", itself tender + swollen counts.

Depth ≤ 3 in practice; the schema below caps traversal at 5 and guards cycles
rather than assuming.

## Data model

### `vocab/composites.yaml` (new file)

The variant layer is additive — `measurements.yaml` is untouched. A composite
measurement keeps its existing id as the concept-level anchor
(`major_adverse_cardiovascular_event`), and variants hang off it with their own
synonyms and patterns. This follows the precedent already set by `concept`:
a new join key, no redesign.

```yaml
version: 1
dimension: composite

variants:
  - id: mace_3point
    measurement_id: major_adverse_cardiovascular_event
    kind: event_union                 # event_union | scored_index | criteria_panel
    label: "3-point MACE"
    definition: >-
      Cardiovascular death, non-fatal myocardial infarction, or non-fatal stroke.
    is_default: true                  # at most one per measurement_id
    synonyms: ["3-point MACE", "three-point MACE", "MACE-3"]
    patterns: ['\b3[- ]point MACE\b', '\bMACE[- ]3\b']
    components:
      - measurement_id: cardiovascular_death
      - measurement_id: myocardial_infarction
      - measurement_id: stroke

  - id: mace_4point
    measurement_id: major_adverse_cardiovascular_event
    kind: event_union
    label: "4-point MACE"
    synonyms: ["4-point MACE", "MACE-4"]
    components:
      - variant_id: mace_3point       # nested: defined against the variant below it
      - measurement_id: heart_failure_hospitalisation

  - id: acr_20
    measurement_id: acr_response_composite
    kind: scored_index
    label: "ACR20"
    is_default: true
    components:
      - measurement_id: tender_joint_count
        role: core_set
        required: true
      - measurement_id: c_reactive_protein
        role: core_set
        required: false               # n-of-m: 3 of 5 remaining core-set measures
    notes: >-
      Decomposition is documentation only. ACR20's threshold (>=20% improvement)
      is already parsed onto the endpoint; component overlap must never be used
      to call two scored indices comparable.
```

A component entry carries **either** `measurement_id` (a leaf) **or**
`variant_id` (a nested composite), never both. Every `measurement_id` must
resolve against `measurements.yaml`; every `variant_id` against this file.

### `vocab.*` tables (written by `vocab validate`)

```
vocab.composite_variants
    variant_id, measurement_id, kind, label, definition, is_default

vocab.composite_components
    variant_id, component_measurement_id, component_variant_id,
    role, required, notes
```

Same pattern as the existing vocab loader: the YAML is source, the tables are
what the pipeline reads. Nothing downstream reads the YAML directly.

### `conformed.endpoint_composite` (written by `conform`)

```
endpoint_id, variant_id, variant_match_method, variant_confidence
```

A separate table, not columns on `conformed.endpoints`, for two reasons: the
large majority of endpoints are not composites, and a composite endpoint whose
variant does not resolve must be representable as *absent* rather than as a
default.

**An unresolved variant must not fall back to `is_default`.** `is_default`
records which variant the literature means by the bare term, for reading — not a
licence to assert it. "MACE" with no point count in the registry text is
genuinely unspecified, and asserting 3-point would fabricate the exact fact
these queries exist to check. This is the same rule `measurements.yaml` already
applies with `on_unmatched: review_queue`, and it matters more here: a wrong
component set doesn't look wrong, it looks like an answer.

## The traversal

Verified against a fixture with the MACE ladder, net clinical benefit, and ACR20
(DuckDB 1.5.5). Expanding a variant to its transitive leaf components:

```sql
WITH RECURSIVE expand(root_variant_id, variant_id, component_measurement_id, depth, path) AS (
    SELECT c.variant_id, c.variant_id, c.component_measurement_id, 1, [c.variant_id]
    FROM vocab.composite_components c
    UNION ALL
    SELECT e.root_variant_id, c.variant_id, c.component_measurement_id, e.depth + 1,
           list_append(e.path, c.variant_id)
    FROM expand e
    JOIN vocab.composite_components p ON p.variant_id = e.variant_id
                                     AND p.component_variant_id IS NOT NULL
    JOIN vocab.composite_components c ON c.variant_id = p.component_variant_id
    WHERE e.depth < 5
      AND NOT list_contains(e.path, c.variant_id)   -- cycle guard
)
SELECT root_variant_id,
       count(DISTINCT component_measurement_id) AS leaf_components,
       max(depth) AS max_depth,
       string_agg(DISTINCT component_measurement_id, ', ') AS components
FROM expand
WHERE component_measurement_id IS NOT NULL
GROUP BY 1;
```

The `path` list is doing real work: `UNION ALL` does not deduplicate, so a
malformed cycle in the YAML would otherwise recurse to the depth cap on every
branch. The validator should reject cycles outright (below), but the query
should not depend on that having happened.

Comparability between two event unions, which is question 2:

```sql
WITH RECURSIVE expand(...) AS ( /* as above */ ),
leaves AS (
    SELECT e.root_variant_id AS variant_id,
           list_sort(list_distinct(list(e.component_measurement_id))) AS components
    FROM expand e
    JOIN vocab.composite_variants v ON v.variant_id = e.root_variant_id
    WHERE e.component_measurement_id IS NOT NULL
      AND v.kind = 'event_union'          -- never scored_index; see above
    GROUP BY 1
)
SELECT a.variant_id AS variant_a, b.variant_id AS variant_b,
       len(list_intersect(a.components, b.components)) AS shared,
       list_filter(a.components, x -> NOT list_contains(b.components, x)) AS only_in_a,
       list_filter(b.components, x -> NOT list_contains(a.components, x)) AS only_in_b,
       round(len(list_intersect(a.components, b.components))::DOUBLE /
             len(list_distinct(list_concat(a.components, b.components))), 2) AS jaccard
FROM leaves a JOIN leaves b ON b.variant_id > a.variant_id
ORDER BY jaccard DESC;
```

`only_in_a` / `only_in_b` matter more than the Jaccard number. "0.8 similar" is
not an analytic finding; "identical except that B includes coronary
revascularisation" is.

## The dependency this needs before anything else

The components mostly **do not exist as measurement terms yet.** Checked against
`measurements.yaml` as it stands:

| component | in `measurements.yaml`? |
|---|---|
| `heart_failure_hospitalisation` | yes |
| `c_reactive_protein` | yes |
| `cardiovascular_death` | no |
| `myocardial_infarction` | no |
| `stroke` | no |
| `coronary_revascularisation` | no |
| `tender_joint_count` / `swollen_joint_count` | no — folded into `acr_response_composite`'s synonyms |
| `major_bleeding` | no — only `annualised_bleeding_rate` |

So decomposition is not "add a components list to five terms". It is **add the
component terms first**, then point at them. That is the bulk of Phase A below, and it
is why the estimate there is vocabulary work rather than code.

Two consequences worth deciding before starting:

* **It is the right change independently.** A trial can and does use "time to
  first myocardial infarction" as an endpoint in its own right, and today that
  either lands on a coarser term or goes to the review queue. The component
  terms are missing measurements, not scaffolding for this spec.
* **It will move existing conform results.** Adding `stroke`,
  `myocardial_infarction` and friends as matchable terms changes what already-
  conformed rows resolve to — `acr_response_composite` currently claims "tender
  joint count" and "swollen joint count" as synonyms, and promoting those to
  their own terms takes rows off it under longest-match. So the Phase A gate
  must be measured **twice**, before and after the component terms land, and the
  delta reported. A coverage change here is a real change in what the warehouse
  says, not a metric moving.

## Phasing, with a gate

**Phase A — vocabulary and measurement only. No pipeline code.**

Add the missing component terms to `measurements.yaml` (see the section
above), write `composites.yaml` for the eight composite-method terms, then
measure against a real pull: of the endpoints resolving to a composite measurement, what
fraction name a variant specifically enough to resolve one?

**This is a gate, not a milestone.** If under ~20% of composite endpoints
specify a variant, stop and record the finding — the registry text does not
carry the information, and the components would be a vocabulary asserting facts
about trials rather than a warehouse recording them. Phase A is roughly a day of
vocabulary work and answers the only question that matters.

Prior expectation is that this fails on `measure` alone and passes on the
cascade into `description`, where protocols spell composites out. That is a
guess, and the gate exists because it is a guess.

**Phase B — resolver and tables**, only if Phase A clears. `composites.yaml`
into `vocab.*`, variant resolution after measurement resolution in the conform
cascade, `conformed.endpoint_composite`, unresolved variants to the review queue
with the same discipline as measurements.

**Phase C — comparability queries** into the cheat sheet, and a
`endpoints composite explain <variant_id>` CLI command printing the expansion.

## Vocabulary defects to fix first

Found while writing this; all in `measurements.yaml`, all cheap now.

* **Three composite-method terms lack `composite: true`** —
  `haematologic_response` and `elf_score` (`laboratory_composite`) and
  `nutritional_status` (`composite_assessment`). Five terms carry the flag,
  eight carry a composite method.
* **`cognitive_composite` is a composite by name, label, and its own `notes`**
  ("a composite is scored across a battery") with neither the flag nor a
  composite method — its method is `computerised_cognitive_battery`.
* **Nothing validates the flag against the method.** `method` is an open
  vocabulary of 59 values with no closed set, so this drifted silently.

Recommendation: **replace the `composite: true` boolean with a
`composite_kind` field** over the closed set `event_union | scored_index |
criteria_panel`, and have `vocab validate` enforce that `composite_kind` is
present iff the term is referenced as a composite. A boolean that says
"something composite is going on" is exactly the conflation this spec argues
against, and the three kinds are already latent in `method`.

That change is worth making whether or not this spec is ever built.

## Validator requirements (Phase B)

`vocab validate` must reject:

* a component naming both `measurement_id` and `variant_id`, or neither
* a `measurement_id` that does not exist in `measurements.yaml`, or that exists
  but is not a composite
* a `variant_id` that does not exist in `composites.yaml`
* **a cycle** in the variant graph, at any depth
* more than one `is_default` per `measurement_id`
* a `kind` outside the closed set
* a variant whose `measurement_id` has no `composite_kind` (after the fix above)
* a synonym claimed by two variants — the existing rule, applied to this file

## Prerequisite

Phase A's gate needs a real pull, and **neither backend has been reachable from
the build sandbox** (AACT port 5432 closed, `clinicaltrials.gov` unreachable);
`vocab/README.md`'s "Known gaps" records the same constraint for the TA
resolver. Phase A cannot be run to completion from an environment with no
egress. Writing `composites.yaml` can; measuring the gate cannot.

## What NOT to do

* **Do not build the §6 node/edge layer alongside this.** If composites land,
  they land as two typed vocab tables and a recursive CTE. A generic node/edge
  encoding would reintroduce every problem in the first section.
* **Do not infer components from endpoint text.** "Composite of death, MI and
  stroke" in a `description` is a tempting parse and a bad one — the exact
  wording varies, adjudication definitions differ, and a wrong component set is
  invisible downstream. Components are curated vocabulary, cited to a
  definition, or they are absent.
* **Do not let an unresolved variant default to `is_default`.** See above.
* **Do not use component overlap on `scored_index` variants** to call two
  endpoints comparable. The `kind` filter in the comparability query is load-
  bearing, not decorative.
* **Do not expand the composite vocabulary to improve coverage numbers.** Same
  rule as `vocab/README.md`: terms come from a human review round against real
  misses, never from chasing a percentage.
