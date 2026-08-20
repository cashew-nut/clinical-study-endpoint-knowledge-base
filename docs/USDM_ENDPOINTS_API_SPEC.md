# Design spec: a USDM 4.0 endpoints API

Status: **proposed, not implemented.** This is a spec to schedule or decide
against, not a plan of record. Nothing described here exists in the tree.

The deliverable is one call:

```
GET /v4/studies/NCT04162249/endpoints
```

returning a USDM 4.0 representation of **every** endpoint in that trial, where
each endpoint's text is a *syntax template* whose tags are bound, through a
`SyntaxTemplateDictionary`, to terms in `vocab/`. The registry string is kept
verbatim alongside it, so the projection is auditable rather than a rewrite.

Read alongside:

* `vocab/README.md` — the vocabulary schema and the judgment calls behind it
* `src/clinical_endpoints/conform/` — the pipeline whose output this projects
* `docs/QUERY_CHEATSHEET.md` — the flat relations this sits on top of
* `docs/COMPOSITE_ENDPOINTS_SPEC.md` — the one structure this spec defers to

---

## What "USDM 4.0" means here, precisely

Checked against the sources rather than assumed, because the version numbering
is misleading:

* `cdisc-org/usdm_api` `main` is at `VERSION = "3.13.0"` and serves `/v3/…`
  routes. The `release-4-0-0` branch sets `VERSION = "4.0.0"` and serves
  `/v4/…`.
* **The pydantic model classes are byte-identical between the two.**
  `git diff main release-4-0-0 -- model` is empty; the only changes are
  `main.py` (routes + version string), the generated `docs/USDM_API.*`, and the
  LICENSE.
* `cdisc-org/DDF-RA` at tag `v4.0.0` agrees: `API_DELTA_3-13-0_4-0-0.csv`,
  `CT_DELTA_3-13-0_4-0-0.csv` and `UML_DELTA_3-13-0_4-0-0.csv` each contain a
  header row and **nothing else**. No class, attribute, relationship or CT term
  changed between 3.13.0 and 4.0.0.

So "implement USDM 4.0" is, for the endpoints module, a commitment to three
things and no more: the `/v4` route prefix, `usdmVersion: "4.0.0"` in the
payload, and conformance to the 3.13.0-era class shapes. That is good news —
it means the model surface below is stable and small — but it should be said
out loud so nobody goes looking for 4.0-specific endpoint semantics that do
not exist.

Both source repos are permissively licensed for what this needs, with one
wrinkle: `usdm_api` `main` is MIT (relicensed under issue #23), but the
`release-4-0-0` branch predates that and still carries **GPL-3.0**. See
[Licensing](#licensing).

## The endpoints module: classes in scope

USDM's endpoint-related classes, with their required attributes (from
`model/*.py` on `release-4-0-0`):

| class | required | notes |
|---|---|---|
| `SyntaxTemplate` (abstract) | `id`, `name`, `text` | plus optional `label`, `description`, `dictionaryId`, `notes` |
| `Endpoint` | `+ purpose` (str), `level` (`Code`), `instanceType` | `purpose` is required and **not present in registry data** |
| `Objective` | `+ level` (`Code`), `instanceType` | holds `endpoints: [Endpoint]` |
| `SyntaxTemplateDictionary` | `id`, `name`, `parameterMaps`, `instanceType` | lives on `StudyVersion.dictionaries` |
| `ParameterMap` | `id`, `tag`, `reference`, `instanceType` | the tag↔value pairing |
| `Code` | `code`, `codeSystem`, `codeSystemVersion`, `decode` | all four required |
| `ExtensionAttribute` | `id`, `url`, `instanceType` | "values or extension attributes, never both" |

In scope: `Objective`, `Endpoint`, `SyntaxTemplate`, `SyntaxTemplateDictionary`,
`ParameterMap`, `Code`, `ExtensionAttribute`.

Out of scope for phase 1: `Estimand`, `AnalysisPopulation`, `IntercurrentEvent`.
They are the natural phase 3 (see [Phasing](#phasing)) — an estimand needs an
analysis population, intervention ids and intercurrent-event handling, none of
which a registry record states.

Explicitly **not** in scope: the write half of the USDM API. `POST`/`PUT
/v4/studyDefinitions` make this a study definitions repository. This is a
read-only projection of registry data and should never pretend otherwise.

## The central idea: form is the sentence, the rest are tags

`forms.yaml` opens by stating that form answers "what kind of number is this
endpoint?", never "what was measured?" — and that keeping form orthogonal to
measurement is the point of the whole dimension. Read that one step further and
it says something stronger:

> **A form *is* a sentence frame.** "Change from baseline in X at T" and
> "Proportion of participants achieving θ in X at T" are not descriptions of
> forms; they are the forms, written out.

Which gives the mapping the entire spec rests on:

```
form_id                    ->  the syntax template  (one per form, curated)
measurement / reference /
timepoint / threshold /
scale / population         ->  the tags that fill it (per endpoint, resolved)
direction                  ->  neither: metadata (see below)
```

`conformed.endpoints` already carries every one of those as a typed column with
a match method and a confidence. The USDM projection is therefore not an
inference step — it is a *rendering* step over decisions the conforming
pipeline already made and recorded. Nothing in this spec re-parses registry
text. If a dimension did not resolve during `conform`, its tag does not resolve
here either, and the template degrades in a defined way rather than guessing.

**Direction is not a tag.** It is derived, never matched (`directions.yaml`),
and it is a property of the endpoint's interpretation, not a constituent of its
name — "decrease is better" belongs in no endpoint sentence. It rides in
`extensionAttributes` instead. Putting it in the text would also make two
identical endpoints render differently in two therapeutic areas, because
`direction_by_ta` exists.

## The template grammar

Templates live in a new vocabulary file and are written as strings, because 18
of them have to stay readable to a human reviewer — that is this repo's whole
ethos. The grammar is tiny and fully specified:

```
template := part*
part     := literal | tag | optional
tag      := '<' NAME '>'                 NAME is [A-Z][A-Z0-9_]*
optional := '[' part+ ']'                dropped entirely if ANY tag inside is unresolved
literal  := any text except < > [ ] \    escape as \< \> \[ \] \\
```

Optional groups may not nest. A tag outside any optional group is **required**:
if it does not resolve, the template does not apply and the endpoint drops a
fidelity tier (see [Fidelity tiers](#fidelity-tiers-every-raw-row-becomes-exactly-one-endpoint)).

Angle brackets are not an aesthetic choice. CDISC CT defines `ParameterMap.tag`
(C207515, "Programming Tag") as *"Character strings bounded by angle brackets
that act as containers for programming language elements"*. Square brackets for
optional groups are then free of collisions, and both delimiters are single
constants in the renderer so a CDISC clarification is a one-line change.

## `vocab/usdm_templates.yaml` (new file)

Same shape as the other vocabulary files: versioned, declarative, human-reviewed,
loaded and validated by `endpoints vocab validate` into `vocab.usdm_templates`.
Like `matching.yaml`, it is not a term list, so it loads through its own path
rather than a `DimensionSpec`.

```yaml
version: 1
kind: usdm_syntax_templates

# Rendered into Endpoint.purpose, keyed on the resolved measurement's `domain`.
# Required by USDM, absent from every registry record -- so it is derived, and
# every endpoint carrying one is flagged `derived: purpose` in its extensions.
purpose_by_domain:
  efficacy:               "To assess the efficacy of the study intervention."
  safety:                 "To assess the safety and tolerability of the study intervention."
  pharmacokinetic:        "To characterise the pharmacokinetics of the study intervention."
  pharmacodynamic:        "To characterise the pharmacodynamics of the study intervention."
  immunogenicity:         "To assess the immunogenicity of the study intervention."
  patient_reported:       "To assess participant-reported outcomes."
  healthcare_utilisation: "To assess healthcare resource utilisation."
  exploratory:            "To explore the effect of the study intervention."
  _default:               "Purpose not stated in the source registry record."

templates:
  - form: responder_proportion
    template: "Proportion of participants achieving <THRESHOLD> in <MEASUREMENT>[ from <REFERENCE>][ at <TIMEPOINT>][ in <POPULATION>]"
    notes: >-
      THRESHOLD is required here because forms.yaml marks this form
      expects_threshold: true. A responder proportion with no recoverable
      threshold is a partial, not a templated, endpoint.
  - form: change_from_baseline
    template: "Change from <REFERENCE> in <MEASUREMENT>[ at <TIMEPOINT>][ (<SCALE>)][ in <POPULATION>]"
    reference_fallback: patient_baseline
  # ... one entry per form id, see the table below
```

### The 18 templates

One per form id in `forms.yaml`. Required tags are outside brackets.

| form | template |
|---|---|
| `time_to_event` | `Time from <REFERENCE> to <MEASUREMENT>[ up to <TIMEPOINT>]` |
| `event_free_rate_at_timepoint` | `Proportion of participants free of <MEASUREMENT> at <TIMEPOINT>[, measured from <REFERENCE>]` |
| `responder_proportion` | `Proportion of participants achieving <THRESHOLD> in <MEASUREMENT>[ from <REFERENCE>][ at <TIMEPOINT>]` |
| `incidence_proportion` | `Proportion of participants with <MEASUREMENT>[ during <TIMEPOINT>]` |
| `proportion_of_time_in_state` | `Proportion of time with <MEASUREMENT> <THRESHOLD>[ during <TIMEPOINT>]` |
| `change_from_baseline` | `Change from <REFERENCE> in <MEASUREMENT>[ at <TIMEPOINT>][ (<SCALE>)]` |
| `percent_change_from_baseline` | `Percent change from <REFERENCE> in <MEASUREMENT>[ at <TIMEPOINT>]` |
| `ratio_to_baseline` | `Ratio to <REFERENCE> of <MEASUREMENT>[ at <TIMEPOINT>]` |
| `shift_from_baseline` | `Shift from <REFERENCE> in <MEASUREMENT> category[ at <TIMEPOINT>]` |
| `value_at_timepoint` | `<MEASUREMENT>[ at <TIMEPOINT>][ (<SCALE>)]` |
| `auc_over_time` | `Area under the <MEASUREMENT> curve[ over <TIMEPOINT>]` |
| `event_count` | `Number of <MEASUREMENT> events[ during <TIMEPOINT>]` |
| `event_rate` | `Rate of <MEASUREMENT>[ per <SCALE>][ during <TIMEPOINT>]` |
| `annualized_rate_of_change` | `Annualised rate of change in <MEASUREMENT>[ from <REFERENCE>][ over <TIMEPOINT>]` |
| `event_free_days` | `Days free of <MEASUREMENT>[ during <TIMEPOINT>]` |
| `correlation` | `Correlation involving <MEASUREMENT>[ at <TIMEPOINT>]` |
| `not_stated` | `<MEASUREMENT>[ at <TIMEPOINT>]` |
| `descriptive` | *(none — always verbatim tier)* |

Two of these deserve a flag rather than a quiet fudge:

* **`correlation` is under-modelled.** A correlation endpoint has two variables;
  `conformed.endpoints` has one `measurement_id`. The template says "involving"
  because the honest alternative — borrowing `<REFERENCE>` for the second
  variable — would misuse a dimension whose `kind` values are `time_origin`,
  `value_reference` and `external_standard`, none of which is "the other thing
  we correlated against". Correlation endpoints render partially by design, and
  fixing it means a schema change, not a template change.
* **`descriptive` has no template on purpose.** `forms.yaml` uses it for rows
  that are not a computable statistic, and `analysable: false` already marks
  them. Generating a confident sentence for registry boilerplate is worse than
  passing the raw string through.

## Tag catalogue

Seven tags. Each resolves to a *rendered value* (what a human reads) and a
*reference* (what the `ParameterMap` points at).

| tag | source | rendered value | reference |
|---|---|---|---|
| `<MEASUREMENT>` | `measurement_id` | `vocab.measurements.label` | `urn:…:vocab:measurement:<id>` |
| `<CONCEPT>` | `measurements.concept` | the concept id, humanised | `urn:…:vocab:concept:<concept>` |
| `<REFERENCE>` | `reference_id` | `vocab.references.label` | `urn:…:vocab:reference:<id>` |
| `<SCALE>` | `scale_id` | `vocab.scales.label` | `urn:…:vocab:scale:<id>` |
| `<TIMEPOINT>` | `timepoint_pattern` + `timepoint_extracted` | rendered per pattern, below | `urn:…:endpoint:<eid>:TIMEPOINT` |
| `<THRESHOLD>` | `threshold_comparator`/`_value`/`_unit` | e.g. `≥75%` | `urn:…:endpoint:<eid>:THRESHOLD` |
| `<POPULATION>` | `raw.design_outcomes.population` | the raw string | `urn:…:endpoint:<eid>:POPULATION` |

References come in exactly two namespaces, and the split matters:

* **Vocabulary references** are stable across every trial in the warehouse and
  resolve to a curated term. They are the reason this design is worth building:
  two trials that both measured HbA1c cite the *same* reference string, which
  is precisely the cross-study join the flat schema already supports, now
  exposed inside a standards-conformant document.
* **Instance references** are per-endpoint literals with no vocabulary term
  (a threshold of 75%, a timepoint of Week 16). They resolve into that
  endpoint's own `extensionAttributes`, so every reference in every dictionary
  is resolvable somewhere.

Serving `GET /v4/vocab/{dimension}/{termId}` makes the first kind resolvable
over HTTP too, which is what turns the dictionary into what CT says it should
be: *"a reference source that provides a listing of valid parameter names and
values"* (C207597).

`<POPULATION>` is AACT-only. The CT.gov API backend always writes `population`
as NULL — the API does not expose it — so under the default `--source
ctgov_api` this tag never resolves and its optional group always drops.

### Labels are not sentence fragments

The obvious rendering rule — "the tag renders as the term's `label`" — breaks
on inspection of `references.yaml`:

```
'First dose / start of treatment'      'Nadir (smallest value on study)'
'Enrolment / informed consent'         'No reference (absolute quantity)'
'Pre-dose value (same day)'            'Reference not determinable'
```

These are *display* labels for a review table, and several are unusable inside
a sentence: "Time from First dose / start of treatment to death" is not
English, and "Change from No reference (absolute quantity) in FEV1" is worse
than no rendering at all.

So every term that can fill a tag needs an **`inline_label`** — the
sentence-fragment form — with `label` as the fallback only where the two
coincide (`Randomisation`, `Screening value`). Two places it could live:

| | in `vocab/*.yaml` per term | in `usdm_templates.yaml` as an override table |
|---|---|---|
| ownership | with the term it describes | with the renderer that needs it |
| churn | touches five vocabulary files | one file |
| reuse | any future renderer gets it free | USDM-specific |

Put it **in the vocabulary files**. An inline form of a term is a fact about
the term, not about USDM, and the alternative is a second parallel term list
that drifts — exactly the failure mode `matching.yaml` exists to prevent.
`vocab validate` then warns (not errors) on any term reachable by a tag whose
`inline_label` is absent and whose `label` contains `/`, `(`, or a leading
capital that is not a proper noun.

Terms whose inline form is *nothing* — `references.yaml`'s `none` and
`not_stated` — declare `inline_label: null`, which makes the tag unresolved and
drops its optional group. That is the correct reading: "no reference" is the
absence of a reference, not a phrase to print.

### Rendering `<TIMEPOINT>`

`timepoint_patterns.yaml` already declares, per pattern, the named capture
groups a parser should populate (`extract:`) — it calls itself the parser spec,
not just a classifier. That makes the rendering table mechanical:

| pattern | extracted | rendered |
|---|---|---|
| `single_fixed` | `value`, `unit` | `Week 16` |
| `visit_window` | `value`, `unit`, `window_pm`, `anchor` | `Week 16 (±3 days)` |
| `anchored_offset` | `value`, `value_end`, `unit`, `anchor` | `Day 1 to Day 28 after first dose` |
| `multi_timepoint` | `values`, `unit`, `has_baseline` | `Weeks 4, 12 and 24` |
| `baseline_to_timepoint` | `value`, `unit` | `Week 52` (the baseline is the `<REFERENCE>`) |
| `bare_duration` | `value`, `unit`, `approximate` | `over 6 weeks` / `over approximately 6 weeks` |
| `cumulative_window` | `start_anchor`, `end_value`, `end_unit` | `from first dose through Week 52` |
| `event_driven` | `estimated_max_value`, `estimated_max_unit` | `until the required number of events (up to 36 months)` |
| `event_relative` | `anchor` | `relative to disease progression` |
| `baseline_only` | — | `baseline` |
| `unspecified` | — | **unresolved** — optional group drops |

`timepoint_raw` is preserved verbatim in the endpoint's extensions regardless,
so a rendering bug is always traceable back to the source string.

Note the interaction `timepoint_patterns.yaml` already warns about: where the
resolved form disagrees with the timepoint category, **trust the form**. The
renderer inherits that rule rather than restating it — `baseline_to_timepoint`
under a `change_from_baseline` form renders the horizon only, because the
baseline is already carried by `<REFERENCE>`.

## The dictionary: one per endpoint

The decision everything else about the dictionary follows from.

`Endpoint.dictionaryId` is `0..1`, and `StudyVersion.dictionaries` is `0..*`.
So tags are resolved *through the endpoint's own dictionary*, which makes the
cardinality a real choice:

| | one dictionary per study version | **one dictionary per endpoint** |
|---|---|---|
| tag names | must be globally unique — `<MEASUREMENT_1>`, `<MEASUREMENT_7>` | short and mnemonic, reused freely |
| endpoint portability | endpoint is meaningless without the study | endpoint + dictionary is self-contained |
| `dictionaries` size | 1 | one per endpoint (~10–30 per trial) |
| diffing two trials | tag indices shift when an endpoint is added | stable |

Take **one dictionary per endpoint**. The tag-index churn under the shared
dictionary is disqualifying on its own: adding one outcome to a trial would
renumber tags in unrelated endpoints, so two pulls of the same trial would
produce diffs everywhere. Offer the shared form as `?dictionary=shared` for
consumers who want a single dictionary, with uniquified tags, and document that
its tag names are not stable identifiers.

## Which attribute carries what

| attribute | content | why |
|---|---|---|
| `text` | **the template, tags unresolved** | CT: text is *"structured text… interspersed with user-defined parameter values"*; a dictionary of tags is pointless if the text has no tags |
| `label` | the fully rendered sentence | CT: *"the short descriptive designation"* — the human reading |
| `description` | **the registry string, verbatim** | CT: *"a narrative representation"* — and it makes the projection auditable |
| `name` | `EP-PRI-01`, `EP-SEC-03`, … | required, non-empty, must be stable; ordinal within (trial, level) |
| `purpose` | from `purpose_by_domain` | required by USDM, absent from registry data, so derived and flagged |
| `level` | CT `Code`, below | |
| `dictionaryId` | this endpoint's dictionary | absent on verbatim-tier endpoints |

The `text`-carries-tags reading is a judgment call and should be recorded as
one. CT C207578 is genuinely ambiguous — "interspersed with user-defined
parameter values" can be read as *text contains the values*. Two things settle
it in favour of tags: `ParameterMap.tag` is defined as an angle-bracketed
string that *acts as a container*, which only makes sense if the container
appears in the text; and a `SyntaxTemplateDictionary` attached to a
fully-resolved sentence would have nothing to resolve. Consumers who want the
resolved sentence read `label`, which is why it is populated for every tier.

## Fidelity tiers: every raw row becomes exactly one Endpoint

The invariant that makes "all endpoints in a trial" honest:

> **Every row in `raw.design_outcomes` for that NCT ID becomes exactly one USDM
> `Endpoint`.** Never fewer. A trial whose endpoints did not conform is a trial
> whose endpoints render less richly — not a trial that appears to have fewer
> endpoints.

This matters because `conform` deliberately routes unresolved rows to
`conformed.review_queue` rather than conforming them at low confidence. Serving
only `conformed.endpoints` would silently drop those rows from the API, and a
consumer counting primary endpoints would get a wrong answer with no signal.
`conformed.review_queue` shares the same `_row_id` content hash, so the two
tables union cleanly on identity.

| tier | condition | `text` | `dictionaryId` |
|---|---|---|---|
| `templated` | conformed; form has a template; every required tag resolved | the template | set |
| `partial` | conformed; some optional groups dropped, or form is `not_stated` | the degraded template | set |
| `verbatim` | review-queue row, or form `descriptive`, or no template applies | the raw `measure` string, no tags | absent |

The tier is carried per endpoint in `extensionAttributes`, and aggregated by a
new `endpoints usdm coverage` command. It should be *measured*, not guessed:
round-two vocabulary coverage is form 76.0%, measurement 64.5%, timepoint
90.8%, population-weighted over the corpus, but those are per-dimension figures
over the vocabulary sample and do not compose into a tier mix — the tiers
depend on joint resolution over the conformed corpus, which no one has counted.
Ship the counter with phase 1 and quote real numbers afterwards.

## Levels: the CT mapping

From `USDM_CT.xlsx` at DDF-RA `v4.0.0` — endpoint levels are codelist C188726,
objective levels C188725:

| registry `outcome_type` | backend | USDM `Endpoint.level` |
|---|---|---|
| `primary` | ctgov_api | C94496 · Primary Endpoint |
| `Primary` | aact | C94496 · Primary Endpoint |
| `secondary` | ctgov_api | C139173 · Secondary Endpoint |
| `Secondary` | aact | C139173 · Secondary Endpoint |
| `other` | ctgov_api | C170559 · Exploratory Endpoint |
| `Other Pre-specified` | aact | C170559 · Exploratory Endpoint |
| `Post-Hoc` | aact | C170559 · Exploratory Endpoint |

**The two backends do not agree on this vocabulary and nothing currently
normalises it.** `ctgov_api.py` writes lowercase `primary`/`secondary`/`other`;
`aact.py` runs `SELECT outcomes.*` and passes AACT's title-case values through
untouched. Any consumer of `raw.design_outcomes.outcome_type` — this API
included — has to normalise, so the mapping above is a case-insensitive lookup
with an explicit unknown-value error, not a `.lower()` and a prefix match. This
is a latent defect in the warehouse, not just an API concern: it is worth
fixing at the ingestion boundary regardless of whether this spec is built.

Objective levels use the parallel codelist: C85826 Primary Objective, C85827
Secondary Objective, C163559 Exploratory Objective.

`codeSystem` and `codeSystemVersion` are required on every `Code` and are
configuration, asserted in exactly one place (`usdm/codes.py`): `codeSystem`
defaults to `"http://www.cdisc.org"`, `codeSystemVersion` to the CT package
date of the DDF-RA release the deployment targets. Confirm both against the CT
package shipped with the target release before the first response goes out —
they are the two fields most likely to be silently wrong.

## Objectives: synthesized, and flagged as such

USDM hangs endpoints off objectives. **Registry records contain no objectives.**
There is no honest way to source them, so they are synthesized — one per level
present in the trial — and every synthesized object carries
`urn:…:derived = "objective"` in its extensions.

The objective's `text` is itself a template, keyed on level, filled from the
distinct measurement *concepts* at that level:

```yaml
objective_templates:
  primary:     "To evaluate the effect of the study intervention on <CONCEPT_LIST>"
  secondary:   "To further evaluate the effect of the study intervention on <CONCEPT_LIST>"
  exploratory: "To explore the effect of the study intervention on <CONCEPT_LIST>"
```

`<CONCEPT_LIST>` renders the distinct `measurements.concept` values of that
level's templated endpoints, capped at three plus "and others", with one
`ParameterMap` per concept (`<CONCEPT_1>`, `<CONCEPT_2>`, …) so each cites its
vocabulary reference. Where no endpoint at that level resolved a measurement,
the objective falls to the verbatim tier: `text` = "Objective not stated in the
source registry record."

`?objectives=none` returns endpoints without the synthesized wrapper, for
consumers who would rather have a gap than a derivation. It is available only
on the module envelope — the wrapper envelope has nowhere to put a
parentless endpoint.

## Identity: deterministic ids

`conform` already refuses random ids: `_row_id` is an MD5 content hash so that
re-running on unchanged input yields the same `endpoint_id`. This API inherits
that rule — every id is UUIDv5 in a fixed namespace over a stable key:

| object | key |
|---|---|
| `Study` | `study:<nct_id>` |
| `StudyVersion` | `version:<nct_id>:1` |
| `Objective` | `objective:<nct_id>:<level>` |
| `Endpoint` | `endpoint:<conformed endpoint_id>` |
| `SyntaxTemplateDictionary` | `dictionary:<endpoint_id>` |
| `ParameterMap` | `pmap:<endpoint_id>:<tag>` |
| `Code` | `code:<codeSystem>:<code>` |

Same warehouse state ⇒ byte-identical response. That is what makes the payload
diffable across pulls, cacheable behind an ETag, and safe to store as a golden
test fixture.

## Extension attributes: the decomposition rides along

`ExtensionAttribute` is USDM's sanctioned escape hatch, and it is exactly the
right vehicle: a standards-only consumer ignores it, while a consumer of *this*
warehouse gets the full decomposition without a second call.

```json
{
  "id": "…", "url": "urn:x-endpoints-kb:usdm:ext:v1:decomposition",
  "instanceType": "ExtensionAttribute",
  "valueExtensionClass": {
    "id": "…", "url": "urn:x-endpoints-kb:usdm:ext:v1:decomposition",
    "instanceType": "ExtensionClass",
    "extensionAttributes": [
      {"url": "…:form",            "valueString": "responder_proportion",  "…": "…"},
      {"url": "…:measurement",     "valueString": "pasi",                  "…": "…"},
      {"url": "…:concept",         "valueString": "psoriasis_severity",    "…": "…"},
      {"url": "…:reference",       "valueString": "patient_baseline",      "…": "…"},
      {"url": "…:direction",       "valueString": "increase_is_better",    "…": "…"},
      {"url": "…:scale",           "valueString": "percentage_of_participants", "…": "…"},
      {"url": "…:timepointPattern","valueString": "single_fixed",          "…": "…"},
      {"url": "…:timepointRaw",    "valueString": "Week 16",               "…": "…"},
      {"url": "…:threshold",       "valueQuantity": {"value": 75.0, "unit": {"…": "AliasCode over percent"}}},
      {"url": "…:matchMethod",     "valueString": "exact",                 "…": "…"},
      {"url": "…:fidelity",        "valueString": "templated",             "…": "…"},
      {"url": "…:analysable",      "valueBoolean": true,                   "…": "…"}
    ]
  }
}
```

Three constraints the implementation must respect: `ExtensionAttribute` carries
*"values or extension attributes, never both"*; `Quantity.unit` is an
`AliasCode`, not a `Code`, so a unit needs the alias wrapper; and every
`Extension` needs an `id`, which follows the UUIDv5 rule above.

## The API surface

### The one call

```
GET /v4/studies/{nctId}/endpoints
```

| parameter | default | meaning |
|---|---|---|
| `envelope` | `module` | `module` \| `wrapper` (see below) |
| `level` | all | `primary` \| `secondary` \| `exploratory`, repeatable |
| `flatten` | `false` | return a flat `endpoints[]` instead of `objectives[].endpoints[]` |
| `dictionary` | `per-endpoint` | `per-endpoint` \| `shared` |
| `objectives` | `synthesized` | `synthesized` \| `none` (module envelope only) |
| `tier` | all | filter to `templated` \| `partial` \| `verbatim` |

Supporting routes:

```
GET /v4/studies/{nctId}/endpoints/{endpointId}    one endpoint + its dictionary
GET /v4/vocab/{dimension}/{termId}                resolves a vocabulary reference
GET /v4/studies/{nctId}/endpoints/coverage        tier mix for this trial
```

**On keying by NCT ID.** USDM's own route is
`GET /v4/studyDefinitions/{studyId}` where `studyId` is a UUID — the identifier
of a study *in a definitions repository*. This project has no such repository;
it has a registry mirror, and the identifier its users hold is an NCT ID. So
the primary route keys on `nctId`, and `/v4/studyDefinitions/{studyId}/endpoints`
is offered as an alias that accepts the UUIDv5 from the identity table below.
The deviation is deliberate and should be documented in the OpenAPI
description, not hidden: a client that has a USDM study UUID from somewhere
else will not find it here.

Responses carry `X-USDM-Version: 4.0.0` and an ETag over the payload hash.

Errors distinguish three states that consumers confuse constantly:

| status | condition |
|---|---|
| 404 | NCT ID not in `raw.studies` — never pulled |
| 200 + empty `objectives` | pulled, but the trial registered no outcomes |
| 409 | pulled, but `conform` has not run — `conformed.*` is empty or stale |

The 409 matters: silently serving every endpoint at verbatim tier because the
pipeline was not run would look like catastrophic vocabulary coverage rather
than a missing build step.

### Why the default envelope is not a `Wrapper`

The obvious design — return USDM's own `Wrapper` — does not survive contact
with the required attributes. Building a schema-valid `Wrapper` from
`raw.studies` forces two clinical assertions the warehouse does not hold:

* `StudyDesignPopulation.includesHealthySubjects: bool` — **required, no
  default.** Every value is a claim about the trial. `raw.studies` has
  `nct_id, phase, overall_status, study_type, start_date,
  primary_completion_date, brief_title, official_title` and nothing about the
  population.
* `InterventionalStudyDesign.model: Code` — **required.** The design model
  (parallel, crossover, …) is available from CT.gov but is not among the eight
  columns `pull` stores.

Plus a handful of required strings (`StudyVersion.versionIdentifier`,
`StudyVersion.rationale`, `StudyDesign.rationale`) that could only be empty.

So the default `envelope=module` returns a small envelope whose *members* are
schema-valid USDM class instances, without pretending to be a study definition:

```json
{
  "usdmVersion": "4.0.0",
  "systemName": "clinical-study-endpoint-knowledge-base",
  "study": {"id": "…uuid5…", "nctId": "NCT04162249"},
  "objectives": [ { "…": "USDM Objective with nested Endpoints" } ],
  "dictionaries": [ { "…": "USDM SyntaxTemplateDictionary" } ],
  "provenance": {
    "source": "ctgov_api", "pullId": "…", "pulledAt": "…",
    "conformedAt": "…", "vocabVersion": "…",
    "tiers": {"templated": 7, "partial": 3, "verbatim": 2}
  }
}
```

`envelope=wrapper` is still offered, for consumers whose tooling only eats
`Wrapper`. It fills the unsourceable attributes with declared placeholders and
lists **every one of them** in `provenance.synthesized[]` and in a
`CommentAnnotation` on the study version. A placeholder that is not announced
is a fabricated clinical fact; the wrapper envelope is acceptable only because
it announces them.

The NCT ID itself has a legal USDM home in either envelope:
`StudyIdentifier(text="NCT04162249", scopeId=<org>)` with an `Organization`
for ClinicalTrials.gov — both required attributes (`text`, `scopeId`) are
sourceable, so this is not a placeholder.

## Worked example

Registry row (`raw.design_outcomes`):

```
outcome_type = "primary"
measure      = "Proportion of participants achieving PASI 75 at Week 16"
time_frame   = "Week 16"
```

Conformed (`conformed.endpoints`): `form_id=responder_proportion`,
`measurement_id=pasi`, `reference_id=patient_baseline`,
`threshold_comparator=">="`, `threshold_value=75`, `threshold_unit=percent`,
`timepoint_pattern=single_fixed`, `timepoint_extracted={"value":16,"unit":"week"}`,
`direction_id=increase_is_better`, `scale_id=percentage_of_participants`.

Projected:

```json
{
  "id": "8f2c…", "name": "EP-PRI-01", "instanceType": "Endpoint",
  "text": "Proportion of participants achieving <THRESHOLD> in <MEASUREMENT> from <REFERENCE> at <TIMEPOINT>",
  "label": "Proportion of participants achieving ≥75% in Psoriasis Area and Severity Index (PASI) from their own baseline at Week 16",
  "description": "Proportion of participants achieving PASI 75 at Week 16",
  "purpose": "To assess the efficacy of the study intervention.",
  "level": {"id": "…", "code": "C94496", "codeSystem": "http://www.cdisc.org",
            "codeSystemVersion": "…", "decode": "Primary Endpoint", "instanceType": "Code"},
  "dictionaryId": "d41a…"
}
```

```json
{
  "id": "d41a…", "name": "EP-PRI-01-DICT", "instanceType": "SyntaxTemplateDictionary",
  "parameterMaps": [
    {"id": "…", "tag": "THRESHOLD",   "reference": "urn:x-endpoints-kb:endpoint:8f2c…:THRESHOLD",   "instanceType": "ParameterMap"},
    {"id": "…", "tag": "MEASUREMENT", "reference": "urn:x-endpoints-kb:vocab:measurement:pasi",      "instanceType": "ParameterMap"},
    {"id": "…", "tag": "REFERENCE",   "reference": "urn:x-endpoints-kb:vocab:reference:patient_baseline", "instanceType": "ParameterMap"},
    {"id": "…", "tag": "TIMEPOINT",   "reference": "urn:x-endpoints-kb:endpoint:8f2c…:TIMEPOINT",   "instanceType": "ParameterMap"}
  ]
}
```

The payoff is the `MEASUREMENT` reference. Every trial in the warehouse that
measured PASI — as a mean change, as PASI75, as PASI90 — cites
`urn:…:vocab:measurement:pasi`, because `measurements.yaml` deliberately keeps
PASI75/90/100 as *one measurement under a threshold* rather than three terms.
Cross-study comparison becomes a string match on a reference inside a
standards-conformant document, which is the whole point of putting the
vocabulary in the dictionary rather than in a sidecar.

## Where the code goes

```
vocab/usdm_templates.yaml          templates, purpose_by_domain, objective_templates
src/clinical_endpoints/usdm/
  codes.py       CT Code constants + codeSystem/version config (one place)
  ids.py         UUIDv5 namespace + key builders
  templates.py   grammar parser, renderer, optional-group elision
  tags.py        tag -> (rendered value, reference) resolution per dimension
  project.py     conformed.endpoints + review_queue -> USDM objects
  envelope.py    module / wrapper envelopes, provenance
  api.py         FastAPI app (phase 2)
  schema/        vendored USDM_API.json from DDF-RA v4.0.0, for validation
```

`vocab/loader.py` gains a `usdm_templates` load path alongside `matching.yaml`'s.
CLI: `endpoints usdm show <NCT_ID> [--envelope …] [-o file.json]`,
`endpoints usdm coverage`, `endpoints serve --port 8000`.

FastAPI and jsonschema are new dependencies; `serve` should be an optional
extra so the pipeline keeps installing without a web stack.

## Validator requirements

`endpoints vocab validate` must reject:

* a `form` in `usdm_templates.yaml` that does not exist in `forms.yaml`
* a form in `forms.yaml` with no template and no explicit
  `verbatim: true` — silent omission is how a form starts rendering as raw text
  without anyone noticing
* a tag not in the seven-tag catalogue
* a malformed template: unbalanced `<>` or `[]`, nested optional groups, a tag
  name outside `[A-Z][A-Z0-9_]*`
* a template with **no** required tag — it would render identically for every
  endpoint of that form
* `THRESHOLD` absent from a template whose form declares `expects_threshold: true`
* a `purpose_by_domain` key outside `MEASUREMENT_DOMAINS`, or a missing `_default`
* a `reference_fallback` naming no term in `references.yaml`
* an objective template whose level is outside the closed set
* an `inline_label` on a term in a dimension no tag can reach

Warnings (not errors): a template referencing `<SCALE>` for a form whose
`typical_scale` is empty; a template longer than 200 characters; a tag-reachable
term with no `inline_label` whose `label` is not sentence-safe.

## Tests

* **Schema conformance.** Validate every projected object against the vendored
  `USDM_API.json` (`Endpoint-Output`, `Objective-Output`,
  `SyntaxTemplateDictionary-Output`). Validate against the JSON Schema, not by
  importing `usdm_api`'s pydantic classes — see [Licensing](#licensing).
* **Tag bijection.** For every endpoint: every tag in `text` has exactly one
  `ParameterMap`, and every `ParameterMap` has exactly one tag in `text`. This
  single property catches most renderer bugs.
* **No residue.** `label` contains no `<`, `>`, `[`, `]` after rendering.
* **Row conservation.** `count(raw.design_outcomes WHERE nct_id = ?)` equals
  the endpoint count in the response, at every tier and every filter default.
* **Determinism.** Two projections of unchanged warehouse state are
  byte-identical.
* **Reference resolvability.** Every `vocab:` reference resolves to a live term
  in `vocab.*`; every `endpoint:` reference resolves into that endpoint's
  extensions.
* **Golden files.** A handful of hand-checked trials spanning all three tiers,
  including one review-queue-only trial and one with zero outcomes.
* **Level normalisation.** Both backends' `outcome_type` vocabularies map, and
  an unknown value raises rather than defaulting to exploratory.

## Phasing

**Phase 1 — the projection, no HTTP.** `usdm_templates.yaml`, the validator
rules, the renderer, `project.py`, `endpoints usdm show`, `endpoints usdm
coverage`. Deliverable: a USDM JSON document on stdout for any pulled and
conformed NCT ID, schema-validated in tests. Everything of substance in this
spec is in phase 1; the HTTP layer is packaging.

*Gate:* the measured tier mix. If `templated` is not the plurality tier on a
real corpus, the templates or the vocabulary need work before an API is worth
serving — and the honest response is to fix the vocabulary, not to loosen the
tiers.

**Phase 2 — the API.** FastAPI app, the routes above, ETag/caching, the vocab
resolution route, `endpoints serve`.

**Phase 3 — estimands.** `Estimand`, `AnalysisPopulation`, `IntercurrentEvent`.
Only worth doing where `population` is present, which means AACT — so it is
gated on AACT reachability, and on being explicit that a registry record does
not state an estimand's intercurrent-event strategy.

**Phase 4 — composite decomposition.** `docs/COMPOSITE_ENDPOINTS_SPEC.md`
introduces component endpoints; once it lands, a composite endpoint's
`<MEASUREMENT>` tag can resolve to a nested structure and the template can
expand components inline. That spec is the prerequisite, not this one.

**Environment constraint, same as everywhere else in this repo:** neither
ingestion backend has been reachable from the build sandbox (AACT port 5432
closed, `clinicaltrials.gov` unreachable). Phases 1 and 2 can be *written* and
unit-tested against fixtures without egress; the phase-1 gate cannot be
measured without a live pull.

## Licensing

* **DDF-RA** (`cdisc-org/DDF-RA`) is MIT for code and scripts, CC BY 4.0 for
  the deliverables. Vendoring `Deliverables/API/USDM_API.json` at tag `v4.0.0`
  into `src/clinical_endpoints/usdm/schema/` is fine with attribution, and is
  the recommended validation path.
* **`cdisc-org/usdm_api`** is MIT on `main` (3.13.0, relicensed under issue
  #23) but **GPL-3.0 on the `release-4-0-0` branch**, which predates the
  relicense. Importing that branch's pydantic classes as a runtime dependency
  would put this repo's licensing in play. Since the 4.0.0 model classes are
  byte-identical to 3.13.0's, there is no technical reason to reach for the
  GPL branch — validate against the DDF-RA JSON Schema instead, and read the
  branch only as documentation.

## Open questions

1. **Does `text` carry tags or values?** CT C207578 admits both readings. This
   spec commits to tags and populates `label` with the resolved sentence so
   neither consumer is stranded. Worth a question to the DDF community before
   phase 2 freezes the contract.
2. **`ParameterMap.reference` has no specified format.** USDM types it as a
   bare string. The URN scheme here is this project's invention; if DDF
   publishes a convention, it should win.
3. **Should vocabulary terms be `BiomedicalConcept`s?** USDM has a whole BC
   subsystem (`StudyVersion.biomedicalConcepts`) that could hold `vocab/`
   terms as first-class USDM objects, making references point at instances
   instead of URNs. That is a bigger, better-integrated design and a much
   larger build; it should be evaluated before phase 3, not after.
4. **Is a synthesized objective better than no objective?** This spec says yes
   with a flag, because USDM requires the containment. A reviewer could
   reasonably prefer `?objectives=none` as the default.

## What NOT to do

* **Do not re-parse registry text in the API layer.** Every dimension is
  resolved by `conform`, with a match method and a confidence recorded. A
  second, subtly different parser inside the projection would be invisible and
  would drift.
* **Do not drop review-queue rows from the response.** A trial with unresolved
  endpoints has those endpoints. Rendering them verbatim is the honest
  degradation; omitting them makes the API quietly wrong about how many
  endpoints a trial has.
* **Do not synthesize `purpose`, objectives, or wrapper placeholders without
  the `derived` flag.** Unflagged synthesis in a standards-conformant document
  is indistinguishable from sourced fact to every downstream consumer.
* **Do not put `<DIRECTION>` in a template.** Direction is derived, can vary by
  therapeutic area, and is not part of an endpoint's name.
* **Do not add vocabulary terms to raise the `templated` share.** Same rule as
  `vocab/README.md`: terms come from a human review round against real misses,
  never from chasing a percentage — and a template that renders confidently
  from a wrong measurement is worse than one that renders verbatim.
* **Do not implement the write half of the USDM API.** This is a projection of
  public registry data. `POST /v4/studyDefinitions` would imply this repo is a
  study definitions repository of record, which it is not.
