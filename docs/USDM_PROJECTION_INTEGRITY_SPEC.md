# Design spec: announced defaults and the USDM extension profile

Status: **proposed.** Companion to `docs/EVENT_SEMANTICS_SPEC.md`, written from
the same first live validation of the USDM projection (NCT01777919,
2026-08-20) and the external USDM review of that payload. The event spec fixes
what the time-to-event sentences *say*; this one fixes how the projection
*accounts for what it says* — defaults, provenance flags, the extension
layout, and the envelope boundary. Nothing here is implemented.

Read alongside:

* `docs/USDM_ENDPOINTS_API_SPEC.md` — the design of record this amends
* `docs/EVENT_SEMANTICS_SPEC.md` — deletes the time-to-event reference
  fallback this spec would otherwise have to announce
* `src/clinical_endpoints/usdm/project.py`, `tags.py`, `envelope.py` — where
  every change below lands

---

## The defects

The wrapper envelope already states the governing principle: **"a placeholder
that is not announced is a fabricated clinical fact."** The validation showed
the endpoint level does not yet live by it.

### 1. A rendered default the document nowhere admits

NCT01777919's primary endpoint carries, in one payload:

```json
"label": "Time from randomisation to Tumour burden (RECIST) over 6 months",
...
{"url": "…:tag:reference",              "valueString": "randomisation"},
...
{"url": "…:decomposition … :reference", "valueString": "not_stated"},
```

The conforming pipeline honestly recorded that the source states no time
origin. The template layer then applied `reference_fallback: randomisation`
(`usdm_templates.yaml`), minted a `tag:reference` host carrying
"randomisation", rendered it into `label` — and flagged nothing. The external
reviewer read this as "the pipeline failed to extract a stated value"; the
truth is worse in one way (the value was *asserted*, not extracted) and better
in another (the decomposition never lied). Three concrete failures:

* **Unannounced synthesis.** The endpoint's own extensions flag `derived:
  purpose` but not the derived reference — so a consumer auditing the payload
  finds one synthesized attribute and misses the other.
* **A contradiction with no arbiter.** Host says randomisation, decomposition
  says not_stated, and nothing in the document or the docs states which
  representation is authoritative or why they may differ.
* **Tier inflation.** The fallback-filled endpoint counts as `templated` —
  "every required tag resolved" — so `endpoints usdm coverage` cannot see how
  much of the templated tier is standing on defaults.

The form-keyed fallback is also simply wrong for part of its range: every
`time_to_event` row got randomisation, including duration-of-response rows
whose origin is first documented response. The event spec deletes that
fallback outright (definitional references via named endpoints, an optional
`{reference}` group for the rest). What remains in scope here is the policy
for the fallbacks that survive, and the announcement machinery any future
default must use.

### 2. The `derived` flag is a constant, not a record

`project.py` appends `derived: purpose` to **every** endpoint unconditionally
and `derived: objective` to every objective. As a contract it is too thin to
audit against: it cannot name a second derived attribute (see above), it
cannot distinguish "derived from the measurement's domain" from "fell through
to `_default`", and an external reviewer misread it — which is evidence about
the flag, not just the reviewer.

### 3. The timepoint dimension is projected below its own semantics

`timepoint_patterns.yaml` already knows that a bare "6 months" is *"an
observation period of that length … NOT an assessment at that timepoint"*,
and that an event-driven duration is *"an administrative estimate … not the
endpoint's timepoint."* The projection flattens all eleven categories to
`timepointPattern`/`timepointRaw` and a rendered phrase — so a consumer must
re-derive from the pattern id what the vocabulary already states, and the
extracted structure (`{"value": 6, "unit": "month"}`, sitting in
`conformed.endpoints.timepoint_extracted`) is not projected at all. The
external review's "observation window" point is right, and the fix is mostly
to stop discarding information the pipeline already has.

### 4. The module envelope does not identify its own boundary

`envelope=module` deliberately serves USDM class instances inside a
non-USDM shape — that was a documented design decision, and `envelope=wrapper`
exists precisely for consumers who need a schema-valid `Wrapper`. But the
module payload itself says nothing machine-readable about which of its keys
are USDM and which are platform envelope; `systemName` is the only hint. The
external reviewer concluded the top level was pretending to be USDM. Half of
that critique misfires — `usdmVersion` and `systemName` are fields *of the
USDM Wrapper itself*, borrowed knowingly — but a boundary that a competent
reviewer cannot find from the artefact alone is underdocumented by
definition.

### 5. Hosts and decomposition have no stated contract

The `tag:*` extension attributes exist because `ParameterMap.reference` must
point at a real instance attribute inside the document (the API spec's
`usdm:ref` constraint) — they are rendering hosts, not a second copy of the
decomposition. But nothing states that: the reviewer read `tag:reference` /
`tag:timepoint` as duplicated source data, and defect 1 shows the two layers
*can* silently disagree, which is exactly what an unstated contract permits.

## The changes

### 1. Fallback policy: definitional or dead, and always announced

A template may declare a fallback only where the **form itself entails the
value** — where the sentence frame's meaning, not corpus convention, supplies
it. That test keeps exactly the change-family `reference_fallback:
patient_baseline` ("Change from baseline in X" whose registry wording omitted
the word "baseline" still *means* change from the participant's own baseline;
the form was matched from that meaning). It kills the deleted time-to-event
fallback for good (`time_to_event` entails *some* origin, not any particular
one — which is why it produced wrong answers for DoR), and it is the bar any
future fallback proposal must argue past.

Every applied fallback is announced on the endpoint (next section), and the
consistency invariant below makes an unannounced one a test failure rather
than a code-review hope.

Tier accounting: a definitional fallback keeps `templated` — the value is a
resolved decision of the vocabulary, announced — but `endpoints usdm
coverage` gains a per-tag defaulted count (`templated: 7, of which reference
defaulted: 3`), so the tier can no longer hide its scaffolding. If a future
measurement shows the defaulted share dominating, that is the trigger to
revisit the tier definition, with data.

### 2. `derived` becomes a per-attribute record

Contract, replacing the current constant:

* One `derived` extension **per synthesized or defaulted attribute** on the
  object that carries it, `valueString` from a closed set the validator
  checks: `purpose`, `reference`, `objective` today; the set grows only by
  spec change.
* Emitted **only when synthesis actually happened**: `purpose` on every
  endpoint (it is always derived from the domain — current behaviour, now
  stated), `reference` only on endpoints whose reference host came from a
  fallback, `objective` on every synthesized objective (all of them, today).
* The decomposition remains the conforming pipeline's truth and is never
  edited to match a rendering: `reference: not_stated` stays in the
  decomposition of a fallback-filled endpoint, the host carries the rendered
  default, and the `derived: reference` flag is the arbiter that says which is
  which. This resolves defect 1's contradiction by *declaring* the
  relationship rather than papering over it.

The external review proposed a richer `derivation` class (method,
confidence). The method and confidence of every matched dimension already
exist in `conformed.endpoints`; they are projected by the profile change
below, where they describe *matching*. `derived` stays a flat flag describing
*synthesis* — two different questions, kept apart on purpose.

### 3. Timepoint role and structure in the decomposition

`timepoint_patterns.yaml` gains a `role` field per pattern, closed set,
validator-checked — vocabulary-owned because the semantics are already
written there in prose:

| role | patterns |
|---|---|
| `assessment_time` | `single_fixed`, `visit_window`, `multi_timepoint`, `baseline_to_timepoint`, `baseline_only`, `anchored_offset`, `event_relative` |
| `observation_window` | `bare_duration`, `cumulative_window` |
| `event_horizon` | `event_driven` |
| `unresolved` | `unspecified` |

The decomposition then carries `timepointRole` alongside
`timepointPattern`/`timepointRaw`, plus the extracted fields the pipeline
already parsed and the projection currently drops: `timepointValue`,
`timepointUnit`, and the pattern-specific extras (`timepointValueEnd`,
`timepointWindowPm`, `timepointAnchor`, …) when present — uniform
`valueString`, the same one-shape rule the API spec already defended for tag
hosts. This is the external review's "structured duration" adopted without a
new duration class: `{"value": 6, "unit": "month"}` was parsed months ago;
it just never left the warehouse.

Rendering is unchanged in this spec — "over 6 months" is defensible English
for an observation window, and the event spec already touches the
time-to-event sentence. A later refinement ("…, assessed over 6 months" for
`observation_window` under event-family forms) becomes a one-line rule in
`render_timepoint` once real payloads justify it.

### 4. Extension profile v2: two classes, one contract

The single `decomposition` block currently mixes three kinds of fact:
what the endpoint *means*, how the pipeline *decided* that, and bookkeeping.
With the event spec adding fields (`event`, `namedEndpoint`,
`eventMatchMethod`) this is the moment to regroup once, under a bumped
namespace — `urn:x-endpoints-kb:usdm:ext:v2:*` — rather than twice:

| extension | carries |
|---|---|
| `decomposition` | semantics: `form`, `event`, `measurement`, `reference`, `direction`, `scale`, `namedEndpoint`, `threshold*`, `timepointPattern`/`Role`/`Raw` + extracted values, `analysable`, `analysisPopulationId` |
| `conformance` | how it was decided: `formMatchMethod`/`Confidence`, `measurementMatchMethod`/`Confidence`, `referenceMatchMethod`/`Confidence`, `eventMatchMethod`/`Confidence`, `fidelity`, `reviewReason`, `sourceRowId` |
| `derived` (n×) | synthesis flags, per attribute (change 2) |
| `tag:*` (n×) | rendering hosts (contract below) |

Confidences join the payload for the first time — they were always in
`conformed.endpoints`, and "which of these decompositions were semantic-
fallback matches" is a question a knowledge-base consumer legitimately asks
of the document itself.

Mapping to the external review's proposed five groups, so the disposition is
explicit: its `decomposition` ≈ `decomposition`; `normalisation` and
`qualityAssessment` ≈ `conformance` (match methods, confidences, fidelity);
`sourceTraceability` ≈ `sourceRowId` here plus the envelope `provenance`
block, which already carries source system and every processing timestamp;
`canonicalEndpoint` ≈ `namedEndpoint` (from the event spec) plus
`sourceRowId` as the durable identity. Five groups collapse to two classes
plus flags because ten extension attributes per endpoint do not need five
containers.

**The host contract, stated at last:** a `tag:<name>` extension exists so a
`ParameterMap` has an in-document attribute to reference; its `valueString`
is the *rendered sentence fragment* (an inline label, a rendered phrase),
while the decomposition carries the *vocabulary id*. The two are different
representations of the same resolved decision, and the invariant is:

> For every tag host on an endpoint, the host value equals the rendering of
> the corresponding decomposition field — unless a `derived` flag names that
> tag, in which case the host carries the announced default.

That invariant is a projection test, run over every fixture payload. It is
what makes defect 1 structurally unrepeatable: a fallback that forgets its
flag now fails CI instead of shipping a quiet contradiction.

### 5. The envelope names its boundary

`envelope=module` keeps its shape — the split between a small module payload
and a schema-valid `Wrapper` is working as designed, and nesting the module's
content under a `studyDefinition` key (the review's proposal) would break
every consumer of the flat shape to duplicate what `envelope=wrapper` already
provides. Three additions make the boundary auditable instead of implied:

* The module payload gains `"profile": "urn:x-endpoints-kb:usdm:module:v2"`
  as its first key — a machine-readable statement that this envelope is the
  knowledge-base module shape, not a USDM class. The wrapper envelope carries
  no profile key; it is the standard's own shape.
* The OpenAPI description and README state the boundary in one sentence each:
  *module = USDM class instances (`objectives[]`, `dictionaries[]`,
  `bcSurrogates[]`, `analysisPopulations[]`) inside a knowledge-base
  envelope (`profile`, `study`, `provenance`); wrapper = canonical USDM.*
  `usdmVersion` and `systemName` are retained in the module deliberately —
  they are the USDM `Wrapper`'s own header fields, and dropping them would
  make the module less standard-adjacent, not more.
* `provenance` is documented as envelope metadata in both envelopes (it
  already sits outside `study` in the wrapper), and gains the per-tag
  defaulted counts from change 1 alongside `tiers`.

## Disposition of the external review

Point by point, so nothing is silently dropped:

| review point | verdict | where |
|---|---|---|
| PFS/OS decompositions clinically wrong; event vs assessment | **accepted** — the priority defect | event spec |
| `tag:*` extensions duplicate the decomposition | **reframed** — they are rendering hosts required by the `usdm:ref` constraint; the real defect was the unstated contract and the silent divergence | change 4/5 here |
| `not_stated` while the label says "randomisation" | **accepted, recaused** — the source genuinely states nothing; the label was an unannounced fallback | event spec deletes it; changes 1–2 govern survivors |
| "over 6 months" is a window, not a timepoint | **accepted** — the vocabulary already says so; project the role and the parsed structure | change 3 |
| replace generic `derived` with contentOrigin/derivation | **adapted** — per-attribute `derived` flags; match method/confidence projected separately in `conformance` | changes 2, 4 |
| envelope: separate USDM payload from platform metadata | **adapted** — boundary made explicit via `profile` + docs; nesting rejected because `envelope=wrapper` already serves that need; `usdmVersion`/`systemName` defended as Wrapper fields | change 5 |
| five-group extension profile | **adapted** — collapsed to `decomposition` + `conformance` + flags + hosts | change 4 |

## Compatibility

One flag day, before any external consumer exists: the extension namespace
moves `v1 → v2` wholesale (decomposition, conformance, derived, `tag:*`, the
wrapper's `masking`/`ageRangeSource`), the module envelope gains `profile`,
and the ETag changes — which it does on any payload change, by design.
Determinism, id ordering, and the ETag's exclusion of `projectedAt` are
untouched. `vocab.usdm_templates` drops the `reference_fallback` column for
`time_to_event` only if the event spec lands first; the two specs share the
flag-day release either way.

## Validator requirements

`endpoints vocab validate` rejects:

* a `timepoint_patterns.yaml` pattern with no `role`, or a `role` outside the
  closed set;
* a `reference_fallback` on a form whose template does not render
  `{reference}`, or on a form outside the change-from-a-reference family —
  the "definitional or dead" test, made mechanical;
* (carried from the event spec) `reference_fallback` on `time_to_event`.

## Tests

* **Host–decomposition consistency** (the invariant in change 4) over every
  fixture payload, including one with an applied `patient_baseline` fallback.
* A fallback-filled endpoint carries `derived: reference`; an endpoint whose
  reference matched from text does not; no endpoint carries an unlisted
  `derived` value.
* `timepointRole` and extracted values appear for each pattern fixture;
  `bare_duration` projects `observation_window` with value 6 / unit month for
  the NCT01777919 row.
* Module envelope: `profile` present and first; wrapper envelope: absent;
  both validate as before against the vendored schema (wrapper) and the
  module's own documented shape.
* `endpoints usdm coverage` reports defaulted-tag counts, and the sum of
  tiers still equals the raw row count.
* Schema conformance, tag bijection, reference resolvability, determinism —
  the standing suite — green under v2 URNs.

## What NOT to do

* **Do not edit the decomposition to agree with a rendering.** The
  decomposition is the pipeline's sworn testimony; renderings that go beyond
  it carry flags.
* **Do not add a fallback that the form's own meaning does not entail.**
  Corpus convention ("TTE endpoints usually start at randomisation") is the
  named-endpoint layer's job, where it is per-definition, cited, and
  conditioned on the study being randomised.
* **Do not silence the defaulted counts to keep `templated` looking high.**
  The count exists to be looked at; the tier definition changes only on
  evidence, by spec.
* **Do not nest the module under a `studyDefinition` key.** Consumers who
  need canonical USDM have `envelope=wrapper`; two half-canonical envelopes
  would be worse than one honest module and one standard Wrapper.
* **Do not bump the extension namespace piecemeal.** v2 lands once, with both
  specs, or not yet.
