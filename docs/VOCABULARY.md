# The vocabulary

## The worked example

The brief sketched a table like this:

| TA | Endpoint | Form | Measurement | Reference | Timepoint | Threshold | Direction | Scale |
|---|---|---|---|---|---|---|---|---|
| Oncology | PFS | Time-to-event | Progression or death | Randomisation | Event-driven | — | Longer = better | Months (KM / HR) |

That sketch is essentially right, and the model here is its formalisation. Three things
changed in the working out.

**"Reference" was carrying two different jobs.** For PFS, "randomisation" is a *time
origin*; for ORR, "baseline sum of diameters" is a *value* the measurement is compared
against. Both are references, but they behave differently, so `reference_type` carries
both kinds with an explicit `temporal_origin` attribute distinguishing them. It also
turned up a subtlety worth recording: under RECIST 1.1, progression is assessed against
the **nadir** — the smallest sum of diameters recorded on study — not against baseline,
while response is assessed against baseline. The same trial uses two different references
for two components of the same endpoint. `best_previous_value` exists for this.

**"Timepoint" was three things.** An *anchor* (from what?), an *offset* (how long?), and
a *selection rule* (which of the participant's assessments?). Keeping them fused makes
"Week 12" look comparable across protocols when it is not: week 12 from randomisation and
week 12 from first dose are different assessment times in any trial with a run-in. And
selection is where ORR hides its real behaviour — best-across-visits, not at a fixed
visit, which makes it structurally optimistic relative to a landmark assessment.

**"Direction" needed four values, not two.** `longer_is_better` is distinct from
`increase_is_better` because the favourable hazard ratio sits *below* one — and
`shorter_is_better` (time to recovery, time to symptom resolution) inverts that again,
which is a routine source of sign errors. There is also a nice inversion in the responder
endpoints: `WEIGHT_LOSS_RESPONDER` has direction `increase_is_better` because the
endpoint is *the proportion of responders*, even though the underlying weight change is
`decrease_is_better`. Holding measurement direction and endpoint direction on separate
axes is what makes that expressible.

Here is the same table as the model actually holds it:

| Concept | Form | Measurement | Reference | Selection | Threshold | Direction | Scale |
|---|---|---|---|---|---|---|---|
| `PFS` | `time_to_event` | `disease_progression` | `randomisation_time` | `first_occurrence` | — | `longer_is_better` | `time_to_event` |
| `ORR` | `responder_binary` | `tumour_burden_recist` | `patient_baseline` | `best_across_timepoints` | `category_attainment` = CR or PR | `increase_is_better` | `proportion` |
| `FEV1_CFB` | `change_from_baseline` | `fev1` | `patient_baseline` | `single_timepoint` | — | `increase_is_better` | `continuous` |
| `HBA1C_CFB` | `change_from_baseline` | `hba1c` | `patient_baseline` | `single_timepoint` | — | `decrease_is_better` | `continuous` |
| `ACR20` | `responder_binary` | `acr_core_component_set` | `patient_baseline` | `single_timepoint` | `composite_criteria` ≥ 20% | `increase_is_better` | `proportion` |

`ORR`'s threshold is `category_attainment` rather than "≥30% decrease" because the
endpoint threshold is *achieving the CR or PR category*; the 30% rule is the underlying
continuous criterion for PR, recorded as a note on the threshold and as a component. The
distinction matters because a study can change the criteria set (RECIST 1.1 → iRECIST)
without changing the endpoint's structure.

`ACR20` is the case that most justifies the model: its threshold applies to **each of
seven components against that component's own baseline**, so a single endpoint carries
seven participant-level baselines. `composite_criteria` records that rather than
flattening it to "≥20% improvement".

---

## The axes

### Defining axes

Change one and it is a different endpoint.

| Axis | Terms | Notes |
|---|---|---|
| `endpoint_form` | 14 | The structural shape. Determines the statistical machinery. |
| `measurement_concept` | 38 | What is observed, before derivation. Carries `modality`, the hook for the future procedure/equipment layer. |
| `reference_type` | 9 | What the value is compared against. The axis most often left implicit in registry text. |
| `direction` | 7 | Includes `non_inferiority`, which is a property of a comparison and so is expected at Layer B. |
| `scale_type` | 8 | Of the *analysed* variable: ORR is a proportion though tumour diameters are continuous. |

### Default axes

Carried by the concept, routinely overridden by a protocol.

| Axis | Terms | Notes |
|---|---|---|
| `summary_measure` | 13 | The fifth ICH E9(R1) estimand attribute. Terms carry `null_value` so effect direction is machine-checkable. |
| `timepoint_selection` | 8 | Includes `sustained_over_window`, materially more stringent than a single assessment. |

### Operational axes

Only the study text can supply these.

| Axis | Terms | Notes |
|---|---|---|
| `timepoint_anchor` | 11 | Aligned to USDM `Timing.relativeToFrom` (codelist C201265). |
| `threshold_kind` | 6 | A 30% relative reduction and a 30-unit absolute reduction share a number and nothing else. |
| `threshold_operator` | 9 | `decrease_by_at_least` is kept distinct from `gte` because the sign convention of the underlying change is a frequent source of error. |
| `analysis_population` | 8 | |
| `endpoint_level` | 3 | Aligned to USDM `Endpoint.level` (codelist C188726), with verified NCI C-codes. |
| `intercurrent_event_strategy` | 6 | The five ICH E9(R1) strategies plus `unspecified`. |

### Supporting axes

`therapeutic_area` (20), `measurement_modality` (11), `unit` (25).

---

## Extending it

### Adding a concept

Add an entry to a file in `vocabularies/concepts/`. The required fields are
`concept_id`, `label`, `definition`, `therapeutic_areas`, the five defining axes under
`structure`, and `status`.

Then add at least one rule in `rules/` so the concept can be observed. A concept with no
rule will never appear in study data and shows up on the Gaps view as an unobserved term.

Run `ceskb validate`. It fails on any unresolved axis term, unknown unit, dangling
related-concept reference, or malformed regex.

### The question to ask before adding a concept

**Is this a different endpoint, or the same endpoint with different parameters?**

`ACR20`, `ACR50` and `ACR70` are separate concepts because the threshold is definitional
— it is in the name, and nobody calls ACR50 "ACR20 with a different threshold". But
"annualised exacerbation rate" and "number of exacerbations" are *one* concept
(`EXACERBATION_RATE`) with a rule that asserts `endpoint_form: count`, because they are
the same measurement analysed differently. Likewise, FEV1 measured at trough, peak or
post-bronchodilator is one concept with a Layer B qualifier, not three concepts.

The bias should be towards fewer concepts and more parameters. Parameters are queryable;
near-duplicate concepts are not.

### Adding an axis

Create `vocabularies/axes/<axis_id>.yaml`. If concepts should carry it, add it to
`STRUCTURE_AXES` in `src/ceskb/vocab/loader.py` with a role of `defining` or `default`.
No database migration is needed.

### Rule authoring

- **Specific before general.** Priority breaks ties; `ACR20` at 260 outranks a generic
  responder pattern at 100.
- **Use `none_of` freely.** Declining to classify is better than classifying wrongly —
  the outcome lands on the Gaps view where it is visible.
- **Set confidence honestly.** The landmark-rate rules sit at 0.55 because the
  distinction between a rate and a time-to-event endpoint is routinely conflated in
  registry titles and deserves review.
- **Use `asserts` rather than a new concept** when a surface form describes the same
  measurement in a different form.
- **Add a test case** to the parametrised table in `tests/test_classify.py`. That table
  is the record of which phrasings are known to map where.
