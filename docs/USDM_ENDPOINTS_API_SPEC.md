# Design spec: a USDM 4.0 endpoints API

Status: **implemented.** This document is the design of record for
`src/clinical_endpoints/usdm/` and `vocab/usdm_templates.yaml`; the sections on
phasing mark what is built and what is deferred.

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

Two constants come from the published example documents rather than from
guesswork, and both are used verbatim by all three: `codeSystem` is
`"http://www.cdisc.org"` and `codeSystemVersion` is `"2024-09-27"`. They stay
configuration in `usdm/codes.py`, but the defaults are now sourced, not
invented.

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

Templates are **authored** in a compact form and **emitted** in USDM's own tag
markup. The two are different on purpose: the authoring form has to stay
readable to a human reviewer of a vocabulary file, and the emitted form has to
match what USDM consumers already parse.

Authoring grammar, as it appears in `vocab/usdm_templates.yaml`:

```
template := part*
part     := literal | tag | optional
tag      := '{' NAME '}'                 NAME is [a-z][a-z0-9_]*
optional := '[' part+ ']'                dropped entirely if ANY tag inside is unresolved
literal  := any text except { } [ ] \    escape as \{ \} \[ \] \\
```

Optional groups may not nest. A tag outside any optional group is **required**:
if it does not resolve, the template does not apply and the endpoint drops a
fidelity tier (see [Fidelity tiers](#fidelity-tiers-every-raw-row-becomes-exactly-one-endpoint)).

Emission follows the convention in CDISC's own published examples
(`DDF-RA/Documents/Examples/`), where a `SyntaxTemplate`'s `text` is an HTML
fragment carrying self-closing tag elements:

```
{measurement}      ->   <usdm:tag name="measurement"/>
whole template     ->   <p>…</p>
```

Verbatim from `CDISC_Pilot_Study.json`, an `EligibilityCriterionItem`:

```json
"text": "<p>Males and postmenopausal females at least <usdm:tag name=\"min_age\"/> years of age.</p>"
```

So the authored template

```
Change from {reference} in {measurement}[ at {timepoint}]
```

emits

```html
<p>Change from <usdm:tag name="reference"/> in <usdm:tag name="measurement"/> at <usdm:tag name="timepoint"/></p>
```

and the corresponding `ParameterMap.tag` values are the bare names —
`reference`, `measurement`, `timepoint` — not the markup. Literal text is
HTML-escaped on emission; the tag elements are not.

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
    template: "Proportion of participants achieving {threshold} in {measurement}[ from {reference}][ at {timepoint}][ in {population}]"
    notes: >-
      `{threshold}` is required here because forms.yaml marks this form
      expects_threshold: true. A responder proportion with no recoverable
      threshold is a partial, not a templated, endpoint.
  - form: change_from_baseline
    template: "Change from {reference} in {measurement}[ at {timepoint}][ ({scale})][ in {population}]"
    reference_fallback: patient_baseline
  # ... one entry per form id, see the table below
```

### The 18 templates

One per form id in `forms.yaml`. Required tags are outside brackets.

| form | template |
|---|---|
| `time_to_event` | `Time from {reference} to {measurement}[ up to {timepoint}]` |
| `event_free_rate_at_timepoint` | `Proportion of participants free of {measurement} at {timepoint}[, measured from {reference}]` |
| `responder_proportion` | `Proportion of participants achieving {threshold} in {measurement}[ from {reference}][ at {timepoint}]` |
| `incidence_proportion` | `Proportion of participants with {measurement}[ during {timepoint}]` |
| `proportion_of_time_in_state` | `Proportion of time with {measurement} {threshold}[ during {timepoint}]` |
| `change_from_baseline` | `Change from {reference} in {measurement}[ at {timepoint}][ ({scale})]` |
| `percent_change_from_baseline` | `Percent change from {reference} in {measurement}[ at {timepoint}]` |
| `ratio_to_baseline` | `Ratio to {reference} of {measurement}[ at {timepoint}]` |
| `shift_from_baseline` | `Shift from {reference} in {measurement} category[ at {timepoint}]` |
| `value_at_timepoint` | `{measurement}[ at {timepoint}][ ({scale})]` |
| `auc_over_time` | `Area under the {measurement} curve[ over {timepoint}]` |
| `event_count` | `Number of {measurement} events[ during {timepoint}]` |
| `event_rate` | `Rate of {measurement}[ per {scale}][ during {timepoint}]` |
| `annualized_rate_of_change` | `Annualised rate of change in {measurement}[ from {reference}][ over {timepoint}]` |
| `event_free_days` | `Days free of {measurement}[ during {timepoint}]` |
| `correlation` | `Correlation involving {measurement}[ at {timepoint}]` |
| `not_stated` | `{measurement}[ at {timepoint}]` |
| `descriptive` | *(none — always verbatim tier)* |

Two of these deserve a flag rather than a quiet fudge:

* **`correlation` is under-modelled.** A correlation endpoint has two variables;
  `conformed.endpoints` has one `measurement_id`. The template says "involving"
  because the honest alternative — borrowing `{reference}` for the second
  variable — would misuse a dimension whose `kind` values are `time_origin`,
  `value_reference` and `external_standard`, none of which is "the other thing
  we correlated against". Correlation endpoints render partially by design, and
  fixing it means a schema change, not a template change.
* **`descriptive` has no template on purpose.** `forms.yaml` uses it for rows
  that are not a computable statistic, and `analysable: false` already marks
  them. Generating a confident sentence for registry boilerplate is worse than
  passing the raw string through.

## Tag catalogue

Six tags. Each resolves to a *rendered value* (what a human reads) and a
*reference* (what the `ParameterMap` points at).

`ParameterMap.reference` is not a free-form URI. CDISC's examples settle it:
every reference in all three is a `usdm:ref` element naming a **class, an
instance id, and an attribute of that instance**, and there are 355–468 of them
per document.

```json
"reference": "<usdm:ref klass=\"Quantity\" id=\"Quantity_9\" attribute=\"value\"></usdm:ref>"
"reference": "<usdm:ref klass=\"StudyDesignPopulation\" id=\"StudyDesignPopulation_1\" attribute=\"description\"></usdm:ref>"
```

That constrains the design hard: **a tag can only reference something that
exists as an instance in the document.** There is no URN escape hatch. So the
projection has to give every tag value a real USDM home:

| tag | rendered from | USDM home | reference |
|---|---|---|---|
| `measurement` | `vocab.measurements.inline_label` | `BiomedicalConceptSurrogate` in `StudyVersion.bcSurrogates` | `klass="BiomedicalConceptSurrogate" attribute="label"` |
| `concept` | `measurements.concept` | same, one per distinct concept | `klass="BiomedicalConceptSurrogate" attribute="label"` |
| `reference` | `vocab.references.inline_label` | `ExtensionAttribute` on the endpoint | `klass="ExtensionAttribute" attribute="valueString"` |
| `timepoint` | rendered per pattern, below | `ExtensionAttribute` on the endpoint | `klass="ExtensionAttribute" attribute="valueString"` |
| `threshold` | `threshold_comparator`/`_value`/`_unit` | `ExtensionAttribute` on the endpoint | `klass="ExtensionAttribute" attribute="valueString"` |
| `scale` | `vocab.scales.inline_label` | `ExtensionAttribute` on the endpoint | `klass="ExtensionAttribute" attribute="valueString"` |

Six, not the seven an earlier draft listed. **The per-outcome analysis
population is not a tag**, because it has no good position in an endpoint
sentence — "…at Week 16 in Safety population" — and its real USDM home is
`Estimand.analysisPopulationId`, which needs the estimand work. It is still
projected: each distinct `population` string becomes an `AnalysisPopulation` on
the study design, linked from the endpoint's decomposition extension. Carried,
not rendered.

Three decisions inside that table are worth defending.

**Measurements become `BiomedicalConceptSurrogate`s, not `BiomedicalConcept`s.**
A `BiomedicalConcept` requires `code: AliasCode` and a `reference` into the
CDISC Library (`/mdr/bc/packages/2025-04-01/biomedicalconcepts/C28421` in the
pilot). `pasi` and `hba1c` are this project's vocabulary, not CDISC Library
concepts, and minting fake C-codes for them would be a fabrication that
survives into every downstream consumer. `BiomedicalConceptSurrogate` is the
class USDM provides for exactly this case — `name`, `label`, `description`, and
an optional free `reference` — and the pilot uses it the same way (`"reference":
"None set"`). Ours carries the vocabulary URI instead of "None set", which is
strictly more information than the reference implementation ships.

**The remaining four have no USDM home, so they get one on the endpoint.**
There is no USDM class for "the baseline this is measured against", "Week 16"
as a bare horizon, or "≥75%". Hosting them as `ExtensionAttribute`s on the
endpoint and referencing `attribute="valueString"` keeps every reference
resolvable inside the document, which is the property the convention actually
requires. It is an extension of the convention rather than an instance of it,
and is flagged as such here so a reviewer can object.

A uniform `valueString` is deliberate even for `threshold`, where
`valueQuantity` would be more expressive: the structured comparator, value and
unit are already in the decomposition extension block, and one reference shape
means one code path in both the projector and any consumer.

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

### Rendering `{timepoint}`

`timepoint_patterns.yaml` already declares, per pattern, the named capture
groups a parser should populate (`extract:`) — it calls itself the parser spec,
not just a classifier. That makes the rendering table mechanical:

| pattern | extracted | rendered |
|---|---|---|
| `single_fixed` | `value`, `unit` | `Week 16` |
| `visit_window` | `value`, `unit`, `window_pm`, `anchor` | `Week 16 (±3 days)` |
| `anchored_offset` | `value`, `value_end`, `unit`, `anchor` | `Day 1 to Day 28 after first dose` |
| `multi_timepoint` | `values`, `unit`, `has_baseline` | `Weeks 4, 12 and 24` |
| `baseline_to_timepoint` | `value`, `unit` | `Week 52` (the baseline is the `{reference}`) |
| `bare_duration` | `value`, `unit`, `approximate` | `over 6 weeks` / `over approximately 6 weeks` |
| `cumulative_window` | `start_anchor`, `end_value`, `end_unit` | `from first dose through Week 52` |
| `event_driven` | `estimated_max_value`, `estimated_max_unit` | `until the required number of events (up to 36 months)` |
| `event_relative` | `anchor` | `relative to disease progression` |
| `baseline_only` | — | `baseline` |
| `unspecified` | — | **unresolved** — optional group drops |

**The rendered phrase carries its own preposition**, and templates therefore
write a bare `[ {timepoint}]`. Putting the preposition in the template instead
produced "during through Week 52" and "up to until the required number of
events" the first time this ran, because a third of the categories render a
self-contained phrase rather than a point in time. Which preposition a category
takes is a property of the category, so it lives with the category.

Where the extracted fields are too thin to render, the raw `time_frame` is used
verbatim, prefixed to read as a phrase: "at" when it opens with a unit label
("Baseline, Week 12 and Week 24"), "over" otherwise, and neither when it
already opens with a preposition. `timepoint_raw` is preserved in the
endpoint's extensions regardless, so a rendering bug is always traceable back
to the source string.

Note the interaction `timepoint_patterns.yaml` already warns about: where the
resolved form disagrees with the timepoint category, **trust the form**. The
renderer inherits that rule rather than restating it — `baseline_to_timepoint`
under a `change_from_baseline` form renders the horizon only, because the
baseline is already carried by `{reference}`.

## The dictionary: one per endpoint

`Endpoint.dictionaryId` is `0..1` and `StudyVersion.dictionaries` is `0..*`, so
tags resolve *through the endpoint's own dictionary* and the cardinality is a
real choice. CDISC's examples take the shared route: `CDISC_Pilot_Study.json`
has two dictionaries, `IE_Dict` (3 parameter maps, shared by four
`EligibilityCriterionItem`s) and `AS_Dict` (2 maps).

That example also shows exactly why the shared route does not generalise to
endpoints. `AS_Dict`'s tags are named `Activity1` and `Activity2` — **numbered,
because one shared namespace cannot hold two different values under one tag
name.** With four to six tags per endpoint and ten to thirty endpoints per
trial, a shared dictionary means `measurement_1 … measurement_30`, and adding a
single outcome renumbers tags in unrelated endpoints, so two pulls of the same
trial diff everywhere.

| | shared, as in `IE_Dict` | **one per endpoint** |
|---|---|---|
| tag names | numbered per instance (`Activity1`, `Activity2`) | stable and mnemonic |
| adding an outcome | renumbers unrelated endpoints | touches one endpoint |
| endpoint portability | meaningless without the study | endpoint + dictionary is self-contained |
| `dictionaries` size | 1 | one per templated endpoint |

Take **one dictionary per endpoint**, named `<endpoint name>_Dict` after the
examples' naming. The shared form stays available as `?dictionary=shared`, with
numbered tags in the CDISC style, and its tag names documented as unstable.

Verbatim-tier endpoints get no dictionary at all: `dictionaryId` is `null`,
which is what every endpoint in all three CDISC examples does today.

## Which attribute carries what

| attribute | content | why |
|---|---|---|
| `text` | **the template, tags unresolved**, as an HTML fragment | what CDISC's own examples do |
| `label` | the fully rendered sentence | CT: *"the short descriptive designation"* — the human reading |
| `description` | **the registry string, verbatim** | CT: *"a narrative representation"* — and it makes the projection auditable |
| `name` | `END1`, `END2`, … | required, non-empty, must be stable; the examples' own convention |
| `purpose` | from `purpose_by_domain` | required by USDM, absent from registry data, so derived and flagged |
| `level` | CT `Code`, below | |
| `dictionaryId` | this endpoint's dictionary, or `null` at verbatim tier | |

The earlier draft of this spec treated "does `text` carry tags or resolved
values?" as an open question, on the grounds that CT C207578 ("structured text…
interspersed with user-defined parameter values") admits both readings. **The
published examples settle it: `text` carries tags.** No inference needed.

Two smaller conventions come from the same examples and are worth matching
rather than inventing around:

* `name` is a short mnemonic code — `END1`/`OBJ1` for endpoints and objectives,
  `IN01` for inclusion criteria — not a slug of the text.
* Unpopulated string attributes are `""`, not `null`, throughout the pilot
  (`"label": ""`, `"purpose": ""`). This projection populates `label` and
  `purpose` on every endpoint, so it never has to choose; but any attribute it
  cannot fill uses `""` to match.

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
`urn:x-endpoints-kb:usdm:ext:v2:derived = "objective"` in its extensions.

The objective's text is templated on level and filled from the distinct
measurement *concepts* at that level:

```yaml
objective_templates:
  primary:     "To evaluate the effect of the study intervention on {concept_list}"
  secondary:   "To further evaluate the effect of the study intervention on {concept_list}"
  exploratory: "To explore the effect of the study intervention on {concept_list}"
  _unresolved: "Objective not stated in the source registry record."
```

**An objective's text is rendered to plain prose, not tag markup, and it
carries no dictionary.** `{concept_list}` names several concepts and a
`ParameterMap` references exactly one instance, so there is nothing for a tag
to point at. Emitting the tag anyway would leave a `<usdm:tag>` in the text
with no matching parameter map — a malformed document. All three CDISC
examples carry plain prose in `Objective.text` for what is presumably the same
reason.

`{concept_list}` renders the distinct concepts of that level's endpoints,
capped at `concept_list_limit` (3) plus "and others". Where no endpoint at that
level resolved a measurement, the objective falls back to `_unresolved`.

## Identity: deterministic ids

CDISC's examples use readable sequential ids — `Endpoint_1`, `Objective_2`,
`Code_622`, `SyntaxTemplateDictionary_1`, `ParameterMap_3` — and this
projection matches that convention rather than emitting UUIDs, which no
published USDM document does.

Sequential ids are only as stable as their ordering, so the ordering is fixed
and content-derived:

| object | ordering key |
|---|---|
| `Endpoint_N` | `(level rank, conformed endpoint_id)` — the content hash, ascending |
| `Objective_N` | level rank: primary, secondary, exploratory |
| `SyntaxTemplateDictionary_N`, `ParameterMap_N` | their endpoint's ordinal, then tag order in the template |
| `BiomedicalConceptSurrogate_N` | distinct measurement id, ascending |
| `Code_N` | first use, in document order |

`conform` already refuses random ids — `_row_id` is a content hash so that
re-running on unchanged input yields the same `endpoint_id`. This inherits
that: same warehouse state ⇒ byte-identical response, diffable across pulls,
cacheable behind an ETag, safe as a golden fixture.

The one thing sequential ids cannot do is survive a *changed* endpoint set:
registering one new outcome shifts every ordinal after it. So the durable
identity — the `endpoint_id` content hash — travels in the endpoint's
extensions, and consumers who need to track an endpoint across pulls are
pointed at that, not at `Endpoint_7`.

## Extension attributes: the decomposition rides along

`ExtensionAttribute` is USDM's sanctioned escape hatch, and it is exactly the
right vehicle: a standards-only consumer ignores it, while a consumer of *this*
warehouse gets the full decomposition without a second call.

**Amended by `docs/USDM_PROJECTION_INTEGRITY_SPEC.md`, now implemented.** The
namespace is `urn:x-endpoints-kb:usdm:ext:v2:*`, and the single block sketched
below splits into two: `decomposition` carries what the endpoint *means* (the
resolved vocabulary ids, `timepointPattern`/`timepointRole`/`timepointRaw` plus
whatever structured fields the pattern parsed, `threshold*`, `analysable`,
`analysisPopulationId`); `conformance` carries how confidently and by what
method each dimension was decided (`formMatchMethod`/`Confidence`,
`measurementMatchMethod`/`Confidence`, `referenceMatchMethod`/`Confidence`,
`eventMatchMethod`/`Confidence`, `fidelity`, `reviewReason`, `sourceRowId`) --
two different questions, kept in two extension classes rather than one. A
`derived` flag rides alongside, once per synthesized or defaulted attribute
(`purpose`, `reference`, `objective`), not a single blanket flag per endpoint.
`threshold` is rendered as a plain string (`"≥75%"`) on both the `tag:threshold`
host and nowhere else in `decomposition` beyond its comparator/value/unit
fields -- there is no `valueQuantity` in the shipped implementation, unlike the
sketch below.

```json
{
  "id": "…", "url": "urn:x-endpoints-kb:usdm:ext:v2:decomposition",
  "instanceType": "ExtensionAttribute",
  "valueExtensionClass": {
    "id": "…", "url": "urn:x-endpoints-kb:usdm:ext:v2:decomposition",
    "instanceType": "ExtensionClass",
    "extensionAttributes": [
      {"url": "…:form",            "valueString": "responder_proportion",  "…": "…"},
      {"url": "…:measurement",     "valueString": "pasi",                  "…": "…"},
      {"url": "…:reference",       "valueString": "patient_baseline",      "…": "…"},
      {"url": "…:direction",       "valueString": "increase_is_better",    "…": "…"},
      {"url": "…:scale",           "valueString": "percentage_of_participants", "…": "…"},
      {"url": "…:thresholdComparator", "valueString": ">=",                "…": "…"},
      {"url": "…:thresholdValue",  "valueString": "75.0",                  "…": "…"},
      {"url": "…:thresholdUnit",   "valueString": "%",                     "…": "…"},
      {"url": "…:timepointPattern","valueString": "single_fixed",          "…": "…"},
      {"url": "…:timepointRole",   "valueString": "assessment_time",       "…": "…"},
      {"url": "…:timepointRaw",    "valueString": "Week 16",               "…": "…"},
      {"url": "…:timepointValue",  "valueString": "16",                    "…": "…"},
      {"url": "…:timepointUnit",   "valueString": "Week",                  "…": "…"},
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
| `flatten` | `false` | return a flat `endpoints[]` instead of `objectives[].endpoints[]` |
| `level` | all | `primary` \| `secondary` \| `exploratory`, repeatable |
| `tier` | all | `templated` \| `partial` \| `verbatim`, repeatable |

Supporting routes:

```
GET /v4/studies/{nctId}/endpoints/coverage     the tier mix for this trial
GET /v4/studies/{nctId}/endpoints/{name}       one endpoint (END1, ...) + its dictionary
GET /v4/vocab/{dimension}/{termId}             resolves a surrogate's `reference`
GET /v4/vocab/concept/{concept}                the measurements sharing one concept
```

That last route is what makes the dictionary what CT says a
`SyntaxTemplateDictionary` should be: *"a reference source that provides a
listing of valid parameter names and values"* (C207597). Every
`BiomedicalConceptSurrogate.reference` this projection emits is a live URL on
the same service.

Responses carry `X-USDM-Version: 4.0.0` and an ETag over the payload hash. The
ETag excludes `provenance.projectedAt` — that is when the response was built,
not what it says, and hashing it would make every request a cache miss.

Errors distinguish three states that consumers confuse constantly:

| status | condition |
|---|---|
| 404 | NCT id not in `raw.studies` — never pulled |
| 200 + empty `objectives` | pulled, but the trial registered no outcomes |
| 409 | pulled, but `conform` has not run — `conformed.*` is empty or stale |

The 409 matters: silently serving every endpoint at verbatim tier because the
pipeline was not run would look like catastrophic vocabulary coverage rather
than a missing build step.

**On keying by NCT id.** USDM's own route is
`GET /v4/studyDefinitions/{studyId}`, where `studyId` identifies a study *in a
definitions repository*. This project has no such repository — it has a
registry mirror, and the identifier its users hold is an NCT id — so the route
keys on that, and the deviation is stated in the OpenAPI description rather
than hidden. A client holding a USDM study UUID from elsewhere will not find it
here.

### The two envelopes

A schema-valid USDM `Wrapper` needs more than the endpoints module. Two of its
required attributes are clinical assertions that the original eight columns of
`raw.studies` simply did not contain:

* `StudyDesignPopulation.includesHealthySubjects: bool` — required, no default.
* `InterventionalStudyDesign.model: Code` — required.

Rather than fill those with placeholders, **the ingestion is being extended to
source them** — see [Where the ingestion has to
grow](#where-the-ingestion-has-to-grow). Both are published fields on both
backends, and CDISC's own `ct-gov_mapping.xlsx` states the mapping, so this is
a data-collection gap rather than a modelling one.

That leaves two envelopes with an honest split:

**`envelope=module`** (default) — the endpoints module and nothing else. Small,
fast, and the right answer for "give me this trial's endpoints": USDM class
instances (`objectives[]`, `dictionaries[]`, `bcSurrogates[]`,
`analysisPopulations[]`) inside a knowledge-base envelope (`profile`, `study`,
`provenance`). Amended by `docs/USDM_PROJECTION_INTEGRITY_SPEC.md`: `profile`
is the first key, stating that boundary machine-readably rather than leaving
it implied by `systemName`, and `provenance` gains `defaulted` alongside
`tiers` -- the per-tag count of endpoints whose value is an announced default,
not something the source actually stated.

```json
{
  "profile": "urn:x-endpoints-kb:usdm:module:v2",
  "usdmVersion": "4.0.0",
  "systemName": "clinical-study-endpoint-knowledge-base",
  "study": {"id": "Study_1", "nctId": "NCT04162249"},
  "objectives": [ { "…": "USDM Objective with nested Endpoints" } ],
  "dictionaries": [ { "…": "USDM SyntaxTemplateDictionary" } ],
  "bcSurrogates": [ { "…": "one per distinct measurement" } ],
  "provenance": {
    "source": "ctgov_api", "pullId": "…", "pulledAt": "…",
    "conformedAt": "…", "vocabVersion": "…",
    "tiers": {"templated": 7, "partial": 3, "verbatim": 2},
    "defaulted": {"reference": 3}
  }
}
```

**`envelope=wrapper`** — a full USDM `Wrapper`, valid against the 4.0.0 schema,
carrying a real `StudyVersion` and `InterventionalStudyDesign` built from the
extended `raw.*`. Canonical USDM: no `profile` key, because it is the
standard's own shape and needs no disclaimer. Anything still unsourceable
(`StudyVersion.rationale`, for one — a registry record has no protocol
rationale) is emitted as `""` and named in `provenance.synthesized[]` and in a
`CommentAnnotation` on the study version. **A placeholder that is not
announced is a fabricated clinical fact**, and that rule does not relax just
because the wrapper now has less to fake.

The NCT ID has a legal USDM home in either envelope:
`StudyIdentifier(text="NCT04162249", scopeId=<org>)` with an `Organization` for
ClinicalTrials.gov — both required attributes are sourceable, so this is not a
placeholder.

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

Authored template (`vocab/usdm_templates.yaml`):

```
Proportion of participants achieving {threshold} in {measurement}[ from {reference}][ at {timepoint}]
```

Projected endpoint:

```json
{
  "id": "Endpoint_1", "name": "END1", "instanceType": "Endpoint",
  "text": "<p>Proportion of participants achieving <usdm:tag name=\"threshold\"/> in <usdm:tag name=\"measurement\"/> from <usdm:tag name=\"reference\"/> at <usdm:tag name=\"timepoint\"/></p>",
  "label": "Proportion of participants achieving ≥75% in the Psoriasis Area and Severity Index from their own baseline at Week 16",
  "description": "Proportion of participants achieving PASI 75 at Week 16",
  "purpose": "To assess the efficacy of the study intervention.",
  "level": {"id": "Code_1", "code": "C94496", "codeSystem": "http://www.cdisc.org",
            "codeSystemVersion": "2024-09-27", "decode": "Primary Endpoint", "instanceType": "Code"},
  "dictionaryId": "SyntaxTemplateDictionary_1",
  "extensionAttributes": [ "… decomposition, and one valueString host per instance tag …" ]
}
```

Its dictionary:

```json
{
  "id": "SyntaxTemplateDictionary_1", "name": "END1_Dict",
  "instanceType": "SyntaxTemplateDictionary",
  "parameterMaps": [
    {"id": "ParameterMap_1", "tag": "threshold",
     "reference": "<usdm:ref klass=\"ExtensionAttribute\" id=\"ExtensionAttribute_2\" attribute=\"valueString\"></usdm:ref>",
     "instanceType": "ParameterMap"},
    {"id": "ParameterMap_2", "tag": "measurement",
     "reference": "<usdm:ref klass=\"BiomedicalConceptSurrogate\" id=\"BiomedicalConceptSurrogate_1\" attribute=\"label\"></usdm:ref>",
     "instanceType": "ParameterMap"},
    {"id": "ParameterMap_3", "tag": "reference",
     "reference": "<usdm:ref klass=\"ExtensionAttribute\" id=\"ExtensionAttribute_3\" attribute=\"valueString\"></usdm:ref>",
     "instanceType": "ParameterMap"},
    {"id": "ParameterMap_4", "tag": "timepoint",
     "reference": "<usdm:ref klass=\"ExtensionAttribute\" id=\"ExtensionAttribute_4\" attribute=\"valueString\"></usdm:ref>",
     "instanceType": "ParameterMap"}
  ]
}
```

and the surrogate it points at, shared by every PASI endpoint in the trial:

```json
{
  "id": "BiomedicalConceptSurrogate_1", "name": "pasi",
  "label": "the Psoriasis Area and Severity Index",
  "description": "Composite clinician-rated score of psoriasis extent and severity…",
  "reference": "/v4/vocab/measurement/pasi",
  "instanceType": "BiomedicalConceptSurrogate"
}
```

The payoff is that surrogate. Every trial in the warehouse that measured PASI —
as a mean change, as PASI75, as PASI90 — emits a surrogate with the same `name`
and the same `reference`, because `measurements.yaml` deliberately keeps
PASI75/90/100 as *one measurement under a threshold* rather than three terms.
Cross-study comparison becomes a join on a reference inside a
standards-conformant document, which is the whole point of putting the
vocabulary in the dictionary rather than in a sidecar.

## Where the ingestion has to grow

Everything above needs one thing the warehouse does not yet have: study-level
design and eligibility facts. `raw.studies` holds eight columns — `nct_id`,
`phase`, `overall_status`, `study_type`, `start_date`,
`primary_completion_date`, `brief_title`, `official_title` — because the
conforming pipeline never needed more.

CDISC publishes the mapping to follow, so this is not a design exercise:
`DDF-RA/Documents/Mappings/ct-gov_mapping.xlsx` maps ClinicalTrials.gov fields
to USDM 4.0.0 paths, with per-field notes. The rows this needs:

| CT.gov field | USDM target | mapping note (from the workbook) |
|---|---|---|
| Interventional Study Model | `InterventionalStudyDesign.model` | "Translate CROSS-OVER to CROSSOVER"; SDTM codelist C99076 |
| Primary Purpose | `InterventionalStudyDesign.subTypes` | |
| Allocation | `StudyDesign.characteristics` | "Set to 'Randomized' if instance with corresponding condition exists" |
| Masking | `StudyRole.code`, `Masking.text` | |
| Enrollment | `StudyDesignPopulation.plannedEnrollmentNumber` | `Quantity` or `Range` |
| Accepts Healthy Volunteers | `StudyDesignPopulation.includesHealthySubjects` | "Set to 'Yes' if true" |
| Sex | `StudyDesignPopulation.plannedSex` | "Map 1 to 1 to corresponding ct.gov terminology" |
| Minimum / Maximum Age | `StudyDesignPopulation.plannedAge` | `Range` of `Quantity`, unit mapped 1:1 |
| Study Population Description | `StudyDesignPopulation.description` | limit 1000 characters |
| Arm Title / Type | `StudyArm.label` / `StudyArm.type` | |
| Study Phase | `StudyDesign.studyPhase` | "Remove 'A' and 'B' from SDTM terminology (C66737)" |

Concretely:

* **`raw.studies` gains** `intervention_model`, `primary_purpose`,
  `allocation`, `masking`, `enrollment_count`, `enrollment_type`,
  `healthy_volunteers`, `gender`, `minimum_age`, `maximum_age`,
  `population_description`.
* **`raw.design_groups` is new** — `nct_id`, `group_type`, `title`,
  `description` — one row per arm.
* Both backends fill both: `ctgov_api` from `protocolSection.designModule`,
  `.eligibilityModule` and `.armsInterventionsModule`; `aact` from
  `ctgov.designs`, `ctgov.eligibilities`, `ctgov.studies.enrollment` and
  `ctgov.design_groups`.

Two things fall out of doing this that are worth having regardless of USDM:

* **`outcome_type` finally gets normalised.** The two backends disagree today
  (lowercase `primary`/`secondary`/`other` versus AACT's `Primary`/`Secondary`/
  `Other Pre-specified`/`Post-Hoc`), and adding a second design-level table is
  the moment to fix the first one rather than propagate it.
* **The population description becomes a reference target.** It is what the
  CDISC pilot's `StudyPopulation` tag points at, so sourcing it is what makes
  the `{population}` tag legal rather than an extension.

This is additive: existing columns keep their names and meanings, `pull` still
upserts, and nothing downstream of `raw.*` changes shape.

## Where the code goes

```
vocab/usdm_templates.yaml            18 templates, purpose_by_domain, objective_templates
vocab/{references,scales,measurements}.yaml
                                     gained `inline_label` on tag-reachable terms
src/clinical_endpoints/ingest/
  design.py      the design/eligibility columns both backends land, and why
src/clinical_endpoints/usdm/
  codes.py       CT codes, codeSystem/version, outcome_type -> level (one place)
  ids.py         ClassName_N ids + the usdm:ref element
  templates.py   grammar parser, renderer, optional-group elision
  tags.py        tag -> (rendered value, reference host); timepoint/threshold rendering
  project.py     conformed.endpoints + review_queue -> USDM objects
  envelope.py    module / wrapper envelopes, provenance, announced placeholders
  api.py         FastAPI app (optional extra)
  schema/        USDM 4.0.0 JSON Schema, vendored from DDF-RA for validation
tests/
  test_usdm_templates.py  grammar
  test_usdm_project.py    invariants + schema conformance
  test_usdm_api.py        routes
```

`vocab/loader.py` gained a `usdm_templates` load path alongside
`matching.yaml`'s, writing `vocab.usdm_templates` / `usdm_purposes` /
`usdm_objective_templates` / `usdm_settings` with each template's tags
pre-computed — the parse happens once, at validate time, not per request.

CLI: `endpoints usdm show <NCT_ID> [--envelope|--flatten|--level|--tier|-o]`,
`endpoints usdm coverage`, `endpoints serve --port 8000`.

FastAPI and uvicorn are an optional extra (`uv sync --extra serve`); nothing
outside `usdm/api.py` imports them, so the pipeline installs without a web
stack and `endpoints serve` says so when it is missing.

## Validator requirements

`endpoints vocab validate` rejects (all implemented in
`vocab/loader.py::_validate_usdm_templates`):

* a `form` in `usdm_templates.yaml` that does not exist in `forms.yaml`
* a form in `forms.yaml` with no template and no explicit
  `verbatim: true` — silent omission is how a form starts rendering as raw text
  without anyone noticing
* a tag not in the closed catalogue (`vocab/schema.py::USDM_TAGS`)
* a malformed template: unbalanced `{}` or `[]`, nested optional groups, an
  empty or tagless optional group, a tag name outside `[a-z][a-z0-9_]*`
* a template with **no** required tag — it would render identically for every
  endpoint of that form
* `THRESHOLD` absent from a template whose form declares `expects_threshold: true`
* a `purpose_by_domain` key outside `MEASUREMENT_DOMAINS`, or a missing `_default`
* a `reference_fallback` naming no term in `references.yaml`
* an objective template whose level is outside the closed set, or that does not
  use exactly one `{concept_list}` tag
* a missing `objective_templates` entry, including `_unresolved`

Warnings (not errors): a template longer than 200 characters; a
`purpose_by_domain` with no entry for some measurement domain; a tag-reachable
term with no `inline_label` whose `label` is not sentence-safe.

That last warning is the one that earned its keep. It fires on a `label`
containing a slash, or a parenthetical that is not a bare abbreviation — so
`Glycated haemoglobin (HbA1c)` passes and `First dose / start of treatment`,
`Percentage of participants (%)` and `Ratio (dimensionless)` do not. It found
50 terms whose display labels would have rendered as broken sentences; all 50
now carry an `inline_label`, and the vocabulary is clean at zero warnings.

## Tests

`uv run pytest` — 254 passing, of which 56 cover this work.

* **Schema conformance.** Every projected object validates against the vendored
  `USDM_API.json` (`Objective-Output`, `SyntaxTemplateDictionary-Output`,
  `BiomedicalConceptSurrogate-Output`, `AnalysisPopulation-Output`), and the
  wrapper envelope against `Wrapper-Output`. Validated against the JSON Schema,
  not by importing `usdm_api`'s pydantic classes — see [Licensing](#licensing).
* **Tag bijection.** For every endpoint: every tag in `text` has exactly one
  `ParameterMap`, and every `ParameterMap` has exactly one tag in `text`. This
  single property catches most renderer bugs.
* **Reference resolvability.** Every `usdm:ref` in every dictionary names an id
  that exists somewhere in the same document — no dangling pointers. Every
  surrogate's `reference` is fetched over HTTP and must return 200.
* **No residue.** `label` contains no `<` or `{` after rendering.
* **Row conservation.** For every trial, the endpoint count equals
  `count(raw.design_outcomes)` — the invariant that makes "all endpoints" true.
* **Determinism.** Two projections of unchanged state are byte-identical.
* **Level normalisation.** Both backends' `outcome_type` vocabularies map, and
  an unknown value raises rather than defaulting to exploratory.
* **Announced placeholders.** The wrapper for a trial with no design data names
  `includesHealthySubjects` and `model` in `provenance.synthesized[]` and in a
  `CommentAnnotation`; the wrapper for a trial *with* design data names neither
  and carries the sourced values.
* **Grammar.** Nine malformed templates are rejected at parse time, and every
  shipped template is asserted to parse and use only known tags.

## Phasing

**Phase 1 — the projection. Built.** `usdm_templates.yaml`, the validator rules,
the renderer, `project.py`, `endpoints usdm show`, `endpoints usdm coverage`.

**Phase 2 — the API. Built.** The FastAPI app, the routes above, ETag caching,
the vocabulary resolution route, `endpoints serve`.

**The ingestion extension. Built.** Both backends now land the design,
eligibility and arm facts, so the wrapper envelope carries sourced values
rather than placeholders wherever the registry states them.

**Phase 3 — estimands. Not built.** `Estimand`, `AnalysisPopulation` as a
variable-of-interest target, `IntercurrentEvent`. `AnalysisPopulation`
instances are already projected, so the missing pieces are the estimand itself
and its intercurrent-event strategy — which a registry record does not state,
making this the point where the honest answer may be "not from this source".

**Phase 4 — composite decomposition. Not built.**
`docs/COMPOSITE_ENDPOINTS_SPEC.md` introduces component endpoints; once it
lands, a composite endpoint's `{measurement}` tag can resolve to a nested
structure. That spec is the prerequisite, not this one.

**The measurement that is still owed.** `endpoints usdm coverage` reports the
tier mix, but no one has run it against a real corpus: **neither ingestion
backend is reachable from this sandbox** (AACT port 5432 closed,
`clinicaltrials.gov` unreachable), the same constraint `vocab/README.md` and
the composite spec record. Every number in this document about tiers comes from
fixtures. The first real pull should run `endpoints usdm coverage` and the
result should replace this paragraph. If `templated` is not the plurality tier,
the fix is the vocabulary, not looser tiers.

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

Two of the four questions in this spec's first draft were answered by reading
CDISC's published example documents rather than by deciding:

* ~~Does `text` carry tags or values?~~ **Tags** — `CDISC_Pilot_Study.json`
  shows `<usdm:tag name="min_age"/>` inside `EligibilityCriterionItem.text`.
* ~~What format is `ParameterMap.reference`?~~ **A `usdm:ref` element** naming
  klass, instance id and attribute. The URN scheme proposed in the first draft
  was wrong and has been removed.

What is still open:

1. **Is `BiomedicalConceptSurrogate` the right home for a vocabulary
   measurement?** It is the closest fit USDM offers, and the pilot uses it for
   concepts with no library definition. The alternative — minting
   `BiomedicalConcept`s with invented C-codes — is worse. But a CDISC reviewer
   may have a third answer, and this is the first design decision to put to
   them.
2. **Is `<usdm:ref klass="ExtensionAttribute" …>` acceptable?** Four of the
   seven tags have no USDM class to point at, so they are hosted as extension
   attributes on their endpoint. Every published reference points at a
   first-class instance instead. This is the spec's one genuine extension of
   the convention.
3. **Nobody has templated an endpoint before.** In all three CDISC examples,
   `dictionaryId` is populated only on `EligibilityCriterionItem` — every
   `Endpoint` and `Objective` has `dictionaryId: null`. The mechanism is
   established; applying it to endpoints is not. That is the opportunity and
   also the risk: there is no reference implementation to match.
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
* **Do not put `{direction}` in a template.** Direction is derived, can vary by
  therapeutic area, and is not part of an endpoint's name.
* **Do not add vocabulary terms to raise the `templated` share.** Same rule as
  `vocab/README.md`: terms come from a human review round against real misses,
  never from chasing a percentage — and a template that renders confidently
  from a wrong measurement is worse than one that renders verbatim.
* **Do not implement the write half of the USDM API.** This is a projection of
  public registry data. `POST /v4/studyDefinitions` would imply this repo is a
  study definitions repository of record, which it is not.
