# Design spec: event semantics for time-to-event endpoints

Status: **implemented.** Written after the first validation of the USDM
projection against a live pull (NCT01777919, 2026-08-20) and an external USDM
review of that payload. The defect it fixes surfaced on the first trial tried,
is systematic rather than incidental, and affects the clinical correctness of
every time-to-event endpoint the warehouse serves.

Read alongside:

* `docs/USDM_ENDPOINTS_API_SPEC.md` — the projection this corrects
* `docs/USDM_PROJECTION_INTEGRITY_SPEC.md` — the companion spec (announced
  defaults, extension profile); written from the same validation
* `vocab/README.md` — the measurement grain this spec deliberately preserves
* `docs/COMPOSITE_ENDPOINTS_SPEC.md` — event unions at measurement level; see
  "Relationship to the composite spec" below

---

## The defect, shown on the first live trial

NCT01777919 registers two outcomes: a primary `measure` of
"Progression-free survival" (`time_frame` "6 months") and a secondary of
"Overall survival" (`time_frame` "2 years"). The pipeline conforms both without
error, at `templated` tier, and projects:

```json
"label": "Time from randomisation to Tumour burden (RECIST) over 6 months",
"description": "Progression-free survival",
...
"label": "Time from randomisation to vital status over 2 years",
"description": "Overall survival",
```

Everything below is behaving exactly as specified — the synonym tables in
`measurements.yaml` name "progression-free survival" under `tumour_burden_recist`
and "overall survival" under `vital_status`, the match method is honestly
`exact`, and the `time_to_event` template faithfully renders
`Time from {reference} to {measurement}`. And the output is clinically wrong,
four distinct ways:

1. **The rendered label asserts a wrong endpoint definition.** PFS is time from
   randomisation to the first of *objective disease progression or death from
   any cause* (FDA Clinical Trial Endpoints guidance; ICH E9). Tumour burden is
   the quantity RECIST measures — the *ascertainment* of the progression
   component, not the event. OS is time to *death from any cause*; "vital
   status" is the ascertainment of death, not the event. `Endpoint.label` is a
   standards-conformant field downstream consumers will treat as an assertion,
   and both assertions are false.

2. **Distinct estimands collapse to one identity.** "PFS", "rPFS", "time to
   progression", "time to response" and "duration of response" are all synonyms
   of `tumour_burden_recist`, and all but TTR/DoR-edge-cases resolve to form
   `time_to_event` — so PFS and TTP conform to *identical* decompositions
   modulo timepoint. TTP censors death; PFS counts it as an event. DoR starts
   the clock at first documented response, not randomisation. The warehouse
   currently asserts these are the same endpoint, which is precisely the
   cross-study comparability trap this project exists to surface (compare
   `forms.yaml`'s insistence on splitting `time_to_event` from
   `event_free_rate_at_timepoint`, and the composite spec's 3-point/5-point
   MACE argument).

3. **Direction derivation already needs the event and gets it through a
   side-channel.** `directions.yaml`'s `event_polarity_cues` block exists
   because "the measurement cannot decide between [progression and response];
   the endpoint text names the event, so the text has to." Those cue regexes
   *are* an event classifier — harm: progression, death, relapse, recurrence…;
   benefit: response, recovery, remission… — whose output is consumed for one
   bit (polarity) and then discarded. The model has the event in three implicit
   places (`event_polarity` on measurements, the cue regexes, event-shaped
   measurement terms like `disease_progression_event`) and in no explicit one.

4. **The synthesized objective inherits the error.** The primary objective for
   the NCT01777919 projection reads "To evaluate the effect of the study
   intervention on tumour burden" — rendered from the measurement's `concept`.
   The trial's primary question is about progression and survival.

A fifth defect surfaced in the same payload — the decomposition says
`reference: not_stated` while the label says "from randomisation", an
unannounced template fallback — and is split into the companion integrity
spec because its scope is wider than time-to-event. This spec removes the
*need* for that fallback on TTE forms; that one governs whatever defaulting
remains.

## Why this is a modelling gap, not a synonym bug

The tempting one-line fix — move "progression-free survival" onto some
event-grained measurement term — breaks a load-bearing, documented design
decision. `measurements.yaml` keeps PFS and ORR on one measurement id *on
purpose*: "that shared id is what makes them a SAME_MEASUREMENT_DIFFERENT_FORM
pair." Regraining the measurement dimension to event level would fix the PFS
sentence by destroying the PFS↔ORR join, trading one correctness property for
another.

The actual gap: **a time-to-event endpoint is defined by a time origin and an
event; the model has dimensions for the origin (`reference`) and for what is
assessed (`measurement`), and none for the event.** So the projection rendered
the only thing it had — the assessment — in the event slot of the sentence.
The fix is a new axis, not a regrained old one:

```
form         what kind of number            time_to_event        (unchanged)
event        what occurrence ends the clock  disease_progression_or_death   (NEW)
measurement  what is assessed / how the      tumour_burden_recist (unchanged --
             event is ascertained                                 the join key)
reference    when the clock starts           randomisation        (unchanged)
```

PFS and ORR still join on `tumour_burden_recist`. PFS and TTP now differ — on
`event`. The sentence renders the event. Nothing downstream of measurement
changes shape.

## What must stay true

Invariants this spec must not break, checked in tests:

* **SAME_MEASUREMENT is preserved.** Every row that resolves
  `tumour_burden_recist` / `vital_status` / `disease_recurrence` today resolves
  the same measurement id after this change. Zero measurement-id churn on
  time-to-event rows is an explicit migration assertion, not a hope.
* **Review-queue discipline is unchanged.** Measurement remains the gate;
  event resolution failing routes nothing to the queue — an unresolved event
  is an explicit `not_stated`, visible and countable, degrading the rendering
  only.
* **The projection stays a rendering step.** Event resolution happens in
  `conform`, with a match method and confidence recorded, never in the API
  layer.
* **Every raw row still becomes exactly one Endpoint**, and two projections of
  unchanged state stay byte-identical.
* **Direction never flips on currently-correct rows.** Event-first derivation
  must reproduce the existing direction on every fixture before it ships.

## `vocab/events.yaml` (new file)

A ninth term-list vocabulary, dimension `event`, same common schema
(`version`, `dimension`, `terms`, ids snake_case, synonyms whole-token per
`matching.yaml`, patterns case-insensitive unless scoped). Two fields are new
to this file and one is borrowed:

```yaml
version: 1
dimension: event
terms:

  - id: death_any_cause
    label: "Death from any cause"
    inline_label: "death from any cause"
    concept: survival
    polarity: harm
    ascertained_by: [vital_status]
    synonyms: ["death", "all-cause death", "death from any cause", "died"]
    patterns:
      - '\btime to (all[- ]cause )?death\b'
      - '\b(all[- ]cause|overall) mortality\b'

  - id: disease_progression
    label: "Objective disease progression"
    inline_label: "disease progression"
    concept: disease_progression
    polarity: harm
    ascertained_by: [tumour_burden_recist, disease_progression_event]
    synonyms: ["disease progression", "objective progression", "radiographic progression",
               "progression of disease", "progressive disease"]
    patterns:
      - '\btime to (disease |radiographic |objective )?progression\b'

  - id: disease_progression_or_death
    label: "Disease progression or death from any cause"
    inline_label: "disease progression or death"
    concept: disease_progression
    polarity: harm
    components: [disease_progression, death_any_cause]
    notes: >-
      The PFS event. An event union: the endpoint fires on whichever component
      occurs first. Components follow the composite spec's event_union
      discipline -- curated, cited, never inferred from text.

  - id: disease_recurrence_or_death
    label: "Disease recurrence or death from any cause"
    inline_label: "disease recurrence or death"
    concept: disease_recurrence
    polarity: harm
    components: [disease_recurrence, death_any_cause]
    notes: The DFS/RFS event.

  - id: response_onset
    label: "Onset of objective response"
    inline_label: "objective response"
    concept: tumour_response
    polarity: benefit
    ascertained_by: [tumour_burden_recist]
    patterns:
      - '\btime to (first |confirmed |objective )?response\b'
  # ... disease_recurrence, treatment_failure, hospitalisation, exacerbation,
  # rescue_medication_initiation, recovery, remission_onset, engraftment, ...
```

* **`polarity`** (`harm` | `benefit`, closed set) replaces the cue lookup for
  rows whose event resolves — this is `event_polarity` moved to where it
  belongs. Measurements keep their `event_polarity` as the fallback for rows
  with no resolved event.
* **`ascertained_by`** names the measurement id(s) that typically ascertain
  the event. It is documentation and a validation target (ids must exist), not
  a matching input — and it is what keeps the PFS↔ORR join *explicable*: the
  PFS event's progression component is ascertained by the measurement ORR is a
  responder proportion over.
* **`components`** makes an event union explicit, event ids only, acyclic,
  same rules as `composites.yaml` components (`event_union` kind is implied;
  there are no scored-index events). A term carries `components` or
  `ascertained_by`+match rules, not both — unions are resolved via named
  endpoints or their own synonyms, never assembled from text.

The starter set is derived from evidence already in the tree, not invented:
every distinct event class named by `directions.yaml`'s harm/benefit cue
regexes, plus the event-shaped measurement terms. Roughly 18–22 terms. Growth
follows the standing rule: from a human review round against real misses,
never to chase coverage.

**`measurements.yaml` gains an optional `implies_event`** on event-shaped
terms — `vital_status → death_any_cause`, `disease_recurrence →
disease_recurrence`, `disease_progression_event → disease_progression`,
`treatment_failure → treatment_failure`, `adverse_event → adverse_event_onset`
(if included) — so a row whose measurement is itself an event resolves that
event with no new text matching. Validated: the id must exist in
`events.yaml`.

## `vocab/named_endpoints.yaml` (new file)

Not a dimension term list — like `matching.yaml` and `usdm_templates.yaml` it
loads through its own path. It states what the literature-named endpoints
*mean*, so that recognising the name resolves the whole bundle instead of
donating the name to one dimension as a synonym.

```yaml
version: 1
kind: named_endpoints
definitions:

  - id: pfs
    label: "Progression-free survival"
    synonyms: ["progression-free survival", "progression free survival", "PFS"]
    form: time_to_event
    event: disease_progression_or_death
    reference: randomisation          # applied only when the study is randomised
    default_measurement: tumour_burden_recist
    citation: "FDA Clinical Trial Endpoints for the Approval of Cancer Drugs and Biologics (2018), Table 4"

  - id: os
    label: "Overall survival"
    synonyms: ["overall survival", "OS"]
    form: time_to_event
    event: death_any_cause
    reference: randomisation
    default_measurement: vital_status
    citation: "FDA (2018): time from randomization until death from any cause"

  - id: ttp
    label: "Time to progression"
    synonyms: ["time to progression", "TTP"]
    form: time_to_event
    event: disease_progression        # deaths censored -- the PFS/TTP difference
    reference: randomisation
    default_measurement: tumour_burden_recist

  - id: dor
    label: "Duration of response"
    synonyms: ["duration of response", "DOR", "DoR"]
    form: time_to_event
    event: disease_progression_or_death
    reference: response_onset         # NOT randomisation; new references.yaml term
    default_measurement: tumour_burden_recist

  - id: dfs
    label: "Disease-free survival"
    synonyms: ["disease-free survival", "disease free survival", "DFS"]
    form: time_to_event
    event: disease_recurrence_or_death
    reference: randomisation
    default_measurement: disease_recurrence

  # rpfs, pfs2, rfs, ttr, ffs, tfst/ttnt follow the same shape.

  - id: efs
    label: "Event-free survival"
    synonyms: ["event-free survival", "EFS"]
    form: time_to_event
    reference: randomisation
    # NO event field: the EFS event set varies by disease and protocol. A
    # definition asserts only what the name stably denotes; the event falls
    # through to the normal cascade or stays not_stated.
```

Rules that make this honest rather than a fabrication engine:

* **A definition carries a field only where the published definition of the
  name pins it.** EFS gets no `event`; a name whose reference varies gets no
  `reference`. Fields a definition does not carry resolve through the normal
  cascade.
* **`reference` from a definition is applied only when `raw.studies.allocation`
  says the trial is randomised** (the column exists since the ingestion
  extension). PFS in a single-arm trial is measured from enrolment or first
  dose; asserting randomisation there would be exactly the unannounced-default
  disease the integrity spec exists to cure. Non-randomised or unknown
  allocation → the definition's reference is skipped, cascade decides,
  `not_stated` stays possible.
* **`default_measurement` yields to the text.** If the measurement cascade
  independently resolves a measurement from the row's own wording ("PFS based
  on PSA progression"), that wins; the default fills silence only. Either way
  the measurement id equals what today's synonym tables produce, which is what
  keeps the zero-churn invariant checkable.
* **`response_onset` is added to `references.yaml`** (`kind: time_origin`,
  inline label "first documented response") — DoR's origin is a real time
  origin the reference vocabulary currently cannot say.

**The synonym migration this forces.** The endpoint-name synonyms and patterns
now living in `measurements.yaml` — "progression-free survival", "PFS",
"rPFS", "time to progression", "TTP", "duration of response", "DOR", "time to
response", "TTR" on `tumour_burden_recist`; "overall survival", "OS", "time to
death" on `vital_status`; "disease-free survival", "DFS", "RFS", "EFS", "LFS"
and the `[- ]free survival` pattern on `disease_recurrence`; "failure-free
survival", "FFS", "TFST", "TSST" on `treatment_failure` — **move out of the
measurement terms** and into named-endpoint definitions (or event terms, for
the non-name event phrases like "time to death"). The measurement terms keep
their assessment-language synonyms (RECIST, tumour response, target lesion,
sum of diameters, mortality-rate phrasings that genuinely name the
ascertainment). The validator's ambiguous-synonym rule extends across the
three files: a synonym claimed by any two of {measurement, event,
named-endpoint} is an error, so the boundary cannot silently regrow.

## Conform changes

`conform_row` gains a step 0 and an event resolution:

```
0. named-endpoint match         measure -> description, whole-token, acronym
                                case rules per matching.yaml. On hit: the
                                definition's fields resolve with
                                match_method = 'named_endpoint'.
1. measurement (as today)       definition's default_measurement fills only
                                if the cascade is silent
2. reference (as today)         definition's reference applies only if the
                                cascade is silent AND allocation is randomised
3. form (as today)              definition's form wins; a cascade disagreement
                                is logged, not honoured
4. event                        (a) definition; (b) events.yaml match over
                                measure -> description; (c) measurement's
                                implies_event; (d) not_stated
5. direction                    event polarity first (resolved event), then
                                event_polarity_cues, then measurement
                                event_polarity, then not_stated
```

Event resolution runs for the **event-family forms**, declared explicitly:
`forms.yaml` gains `event_family: true` on the six forms whose statistic is
about a defined occurrence — `time_to_event`, `event_free_rate_at_timepoint`,
`event_free_days`, `incidence_proportion`, `event_count`, `event_rate`. An
explicit flag rather than a derivation from `direction_rule`, because the
direction rules do not carve this joint: `event_free_rate_at_timepoint` is
`higher_count_better` (a free-rate's direction is fixed whatever the event's
polarity) yet is entirely about an event, while `shift_from_baseline` is
`inherit_event_polarity` yet names none. For non-event-family forms
`event_id` is NULL, not `not_stated` — a change-from-baseline endpoint does
not have an unresolved event; it has no event.

Schema: `conformed.endpoints` gains

```
event_id VARCHAR, event_match_method VARCHAR, event_confidence DOUBLE,
event_source_field VARCHAR, named_endpoint_id VARCHAR
```

`matching.yaml` provenance gains the method and its floor:

```yaml
match_method: [exact, syntactic_rule, semantic, named_endpoint]
confidence_floor:
  named_endpoint: 0.9   # identification is exact; the expansion is curated
                        # vocabulary, not text -- distinguishable and slightly
                        # below exact on purpose
```

A cascade query that today excludes inferred forms with
`WHERE form_match_method = 'exact'` keeps working; one that wants
definition-derived rows in or out can now say so.

Direction consolidation is deliberate scope: with events first-class, the
harm/benefit cue regexes in `directions.yaml` become the fallback layer for
unresolved events rather than the primary event evidence. They are not
deleted — rows whose event does not resolve still need them — but the file
gains a note that new event classes are added to `events.yaml`, not to the cue
lists, so the two cannot drift apart.

## USDM projection changes

**`{event}` joins the tag catalogue** (`vocab/schema.py::USDM_TAGS` stays
closed; this is its one addition). Its home follows `measurement`: a
`BiomedicalConceptSurrogate` per distinct resolved event, `reference`
`/v4/vocab/event/{id}`, served by the existing vocabulary route with `event`
as a new dimension. Inline labels render the sentence fragment ("death from
any cause", "disease progression or death").

Templates change for the forms whose sentence names an event:

| form | template |
|---|---|
| `time_to_event` | `Time[ from {reference}] to {event}[ {timepoint}]` |
| `event_free_rate_at_timepoint` | `Proportion of participants free of {event} {timepoint}[, measured from {reference}]` |
| `event_free_days` | `Days free of {event}[ {timepoint}]` |

Three consequences, each intended:

* **`{reference}` becomes optional in `time_to_event`, and
  `reference_fallback: randomisation` is deleted.** A TTE row whose origin is
  stated or definition-resolved renders "Time from randomisation to death from
  any cause"; one whose origin is genuinely unresolved renders "Time to death
  from any cause" — grammatical, and asserting nothing the source did not
  state. This is what removes the `not_stated`-in-decomposition /
  "randomisation"-in-label contradiction for TTE rows: the head cases (PFS,
  OS, DFS…) get a *definitional* reference recorded as `named_endpoint` in the
  decomposition, and the tail stops being silently defaulted at render time.
* **`{event}` is required.** An event-family row whose event does not resolve
  must not render the assessment in the event slot — "Time from randomisation
  to Tumour burden (RECIST)" is the confident-and-wrong rendering this whole
  spec exists to kill. It degrades to the `not_stated` frame
  (`{measurement}[ {timepoint}]` → "Tumour burden (RECIST) over 6 months") at
  `partial` tier: less pretty, asserts only what resolved.
* `incidence_proportion` / `event_count` / `event_rate` keep `{measurement}`
  until phase D. Their corpus is AE-dominated, where the measurement *is* the
  event and current renderings read correctly; switching them waits on
  measured event coverage.

Everything else that touches the payload:

* The decomposition extension gains `event`, `eventMatchMethod`, and (when
  step 0 hit) `namedEndpoint`. The integrity spec's profile-v2 regrouping
  carries them.
* **The measurement surrogate is minted for every conformed endpoint with a
  resolved measurement**, tag-referenced or not. Today a surrogate exists only
  because a `{measurement}` tag points at it; with TTE sentences using
  `{event}`, PFS rows would otherwise stop carrying the
  `tumour_burden_recist` surrogate — silently dropping the cross-study join
  from the USDM document, the one thing the API spec calls "the payoff".
* Objectives draw their concept list from the **event's** concept for
  event-family endpoints, the measurement's otherwise. The NCT01777919 primary
  objective becomes "To evaluate the effect of the study intervention on
  disease progression" rather than "on tumour burden".

## Worked example: NCT01777919 after this spec

Row: `measure = "Progression-free survival"`, `time_frame = "6 months"`,
allocation randomised.

Conformed: `named_endpoint_id = pfs`; `form_id = time_to_event`
(`named_endpoint`); `event_id = disease_progression_or_death`
(`named_endpoint`); `measurement_id = tumour_burden_recist`
(`named_endpoint` — cascade silent, default filled); `reference_id =
randomisation` (`named_endpoint`); `direction_id = longer_is_better` (event
polarity, no cue regex consulted); timepoint unchanged (`bare_duration`,
"6 months").

Projected:

```json
"label": "Time from randomisation to disease progression or death over 6 months",
"description": "Progression-free survival",
"text": "<p>Time from <usdm:tag name=\"reference\"/> to <usdm:tag name=\"event\"/> <usdm:tag name=\"timepoint\"/></p>"
```

with the decomposition now *agreeing* with the label (`reference:
randomisation`, method `named_endpoint`), the event surrogate carrying
`/v4/vocab/event/disease_progression_or_death`, and the
`tumour_burden_recist` surrogate still present for the ORR join. The OS row
renders "Time from randomisation to death from any cause over 2 years". TTP
and DoR rows, when they arrive, stop being PFS's twins.

## Migration and measurement

`conform` is a wholesale refresh, so there is no data migration — there is a
**delta to measure and publish**, before/after on the same pulled corpus:

1. **Zero measurement-id churn** on event-family rows (assertion, not a
   metric). Match *methods* on those rows change `exact → named_endpoint`;
   that is the provenance getting more honest, and the release note says so.
2. **Event coverage**: fraction of event-family rows with a resolved event,
   by resolution path (definition / text / implied / not_stated). This is the
   gate: if text+implied resolution is weak outside the named-endpoint head,
   the events vocabulary needs a review round before the template flip ships —
   flipping `{event}` to required while coverage is poor would crater the
   `templated` tier for oncology trials.
3. **Tier mix** before/after, from `endpoints usdm coverage`. Expect
   `templated` to *drop* slightly even at good event coverage: rows that were
   templated only because the reference fallback filled the hole become
   honest partials. That drop is the metric working, not regressing — same
   rule as round two's coverage figures being "lower and meaning more".
4. **Direction regression**: zero direction changes across the existing
   fixture suite.

Neither ingestion backend is reachable from this sandbox (the standing
constraint recorded in `vocab/README.md` and both existing specs), so the
coverage gate needs the first environment that can pull. The vocabulary and
conform work does not wait on it; the template flip does.

## Phasing

**Phase A — vocabulary only.** `events.yaml`, `named_endpoints.yaml`,
`implies_event` on the event-shaped measurements, `event_family` on the six
forms, `response_onset` in `references.yaml`, the synonym migration out of
`measurements.yaml`, and every validator rule below. Runs entirely in this
sandbox; `vocab validate` green is the exit.

**Phase B — conform.** Step 0, event resolution, the new
`conformed.endpoints` columns, event-first direction. Exit: the zero-churn
and direction-regression assertions green on the fixture corpus.

**Phase C — projection.** The `{event}` tag and its surrogate, the three
template changes, the degraded frame, always-minted measurement surrogates,
objectives from event concepts, `/v4/vocab/event/…`. **Gated on the event
coverage measurement from a real pull** (point 2 above): the template flip
ships only once event coverage on event-family rows supports it.

**Phase D — extensions, evidence-scheduled.** `{event}` in the
incidence/count/rate templates; named endpoints beyond time-to-event (the
ORR family); the `free_phrase` refinement. Each on measured need, none
blocking A–C.

## Validator requirements

`endpoints vocab validate` rejects:

* `events.yaml`: duplicate/malformed ids; `polarity` outside {harm, benefit};
  a `components` entry naming a missing event or forming a cycle; a term with
  both `components` and synonyms/patterns; an `ascertained_by` naming no
  measurement; a synonym claimed by two events.
* `measurements.yaml`: an `implies_event` naming no event term.
* `named_endpoints.yaml`: a `form`/`event`/`reference`/`default_measurement`
  naming no term in its file; a definition with no `form`; a synonym or
  pattern claimed by another definition; **a synonym claimed by both a
  definition and a measurement or event term** (the cross-file rule that keeps
  the migration from regrowing); a TTE definition carrying `reference` but no
  `citation`.
* `forms.yaml`: `event_family` present and boolean; a warning (not an error)
  where a form declares `time_polarity` or `inherit_event_polarity` without
  `event_family: true`, so the two properties cannot drift apart unnoticed.
* `usdm_templates.yaml`: `{event}` used by a form outside the event family;
  a `time_to_event` template still rendering `{measurement}` once phase C
  (below) is enabled.

Warnings: an event term reachable by the `{event}` tag with no sentence-safe
label (the existing `inline_label` check, applied to the new file);
`event_polarity_cues` naming an event class no `events.yaml` term covers.

## Tests

* NCT01777919 fixture (the two rows above): expected decomposition, label,
  text, dictionary, surrogates — the regression test this incident earns.
* PFS vs TTP vs DoR fixtures conform to three distinct (event, reference)
  pairs; PFS and ORR fixtures still share `measurement_id`.
* Single-arm PFS fixture: definition reference *not* applied; reference
  resolves from text or stays `not_stated`; label renders "Time to …".
* Event-unresolved TTE fixture renders the `not_stated` frame at `partial`,
  never the measurement in the event slot.
* Direction: every existing direction fixture unchanged under event-first
  derivation.
* Tag bijection, reference resolvability, determinism, row conservation, and
  schema conformance — the existing invariant suite — over payloads containing
  `{event}`.
* Validator: each new rejection above has a failing-fixture test.

## Relationship to the composite spec

`disease_progression_or_death` is an event union, and
`docs/COMPOSITE_ENDPOINTS_SPEC.md` models event unions — at *measurement*
level, for composite measurements like MACE. The two do not collide: this
spec's unions are the small, curated set of endpoint-defining events for the
event slot of a sentence (single digits of terms, depth 1); the composite
spec's variant machinery handles composite measurements with laddered
variants. The discipline is shared — components curated and cited, never
inferred from text, no default variant asserted — and if the composite spec
lands, unifying the two component representations is its Phase B question,
noted there rather than duplicated here. A trial whose TTE endpoint is "time
to first MACE" resolves `event: mace` with `ascertained_by:
[major_adverse_cardiovascular_event]`, and the component expansion stays the
composite spec's job.

## Open questions

1. **Does `event` deserve concept promotion?** Events carry a `concept` field
   reusing the measurement concept namespace (survival, disease_progression).
   If concepts grow definitions of their own, the promotion path in
   `vocab/README.md` decision #2 applies unchanged.
2. **`event_free_rate_at_timepoint` phrasing.** "Proportion of participants
   free of death from any cause at Year 2" is correct and stilted; a per-event
   `free_phrase` ("alive") would read better and is one more curated field.
   Deferred until the template flip is measured.
3. **Phase-D scope.** Extending named endpoints beyond TTE (ORR, DCR, pCR
   rate as responder/incidence definitions with thresholds) reuses this file
   unchanged, but the value is lower — those sentences are not wrong today,
   merely thresholdless. Schedule on evidence.

## What NOT to do

* **Do not regrain `measurements.yaml` to event level.** The PFS↔ORR
  same-measurement pair is the point of the current grain; the event is a new
  axis, not a better measurement.
* **Do not resolve events in the projection layer.** `conform` resolves, with
  method and confidence; the projection renders. The API-spec rule against a
  second parser stands.
* **Do not let a definition assert what the name does not pin** — no event on
  EFS, no reference on a non-randomised trial, no components inferred from
  registry prose.
* **Do not keep `reference_fallback` on `time_to_event` "for coverage".** A
  fallback keyed on form asserts randomisation for DoR rows, which is wrong,
  silently. The definitional path asserts it only where the definition does.
* **Do not add event terms to raise event coverage.** Same rule as every
  vocabulary: terms come from review rounds against real misses.
* **Do not render `{measurement}` in the event slot as a fallback.** The
  degraded frame exists precisely so the wrong confident sentence never ships
  again.
