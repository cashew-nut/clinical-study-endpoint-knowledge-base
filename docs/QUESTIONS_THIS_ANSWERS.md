# Questions this answers

The rest of the documentation describes the machinery: the vocabularies, the
conformance engine, the warehouse, the USDM projection. This page is the other
direction -- six questions a clinical, biostatistics or study-design team
actually asks, and what running the pipeline gives back for each.

Each one names who asks it, what they do today instead, the commands that
answer it, and the shape of the answer. Every example below was executed; see
[How these were validated](#how-these-were-validated) for what that did and did
not establish.

* [1. What variability should I assume when sizing this trial?](#1-what-variability-should-i-assume-when-sizing-this-trial)
* [2. What is the endpoint convention in this indication?](#2-what-is-the-endpoint-convention-in-this-indication)
* [3. Which trials measured the same thing a different way?](#3-which-trials-measured-the-same-thing-a-different-way)
* [4. What non-inferiority margin has precedent here?](#4-what-non-inferiority-margin-has-precedent-here)
* [5. Did trials report the endpoints they registered?](#5-did-trials-report-the-endpoints-they-registered)
* [6. Can our endpoint text leave the registry as CDISC USDM 4.0?](#6-can-our-endpoint-text-leave-the-registry-as-cdisc-usdm-40)

---

## 1. What variability should I assume when sizing this trial?

**Who asks.** The trial statistician writing the sample-size section of a
protocol. A power calculation on a continuous endpoint needs an SD; the effect
size is a clinical assumption the team argues about, but the SD is supposed to
be an empirical fact, and there is no published prior for it. In practice it
gets reconstructed by hand from two or three papers someone happens to know,
and the resulting `n` inherits whatever those papers happened to enrol.

**What the project does instead.** The results section is already sitting on the
registry for every trial that posted one. `results conform` reads it, normalises
whatever that sponsor called "dispersion" -- SD, standard error, 95% CI,
inter-quartile range -- into one estimated SD, and `endpoints stats` reports the
distribution across every trial that measured the same thing the same way.

```bash
uv run endpoints stats --measurement fev1
```

```
measurement=fev1, source=outcome

  change_from_baseline · litres  (converted via scales.yaml)
    studies 4      arms 11     participants 4,264
    SD      median 0.2982   IQR 0.2782-0.4947   range 0.259-0.5758
            reported 5 · from_standard_error 4 · from_confidence_interval 2
    timepoints  baseline_to_timepoint (4)
    coverage    4 of 4 conformed studies reported a usable dispersion (100.0%)
```

Three things in that output are the reason it is worth having:

* **The grouping is `form` x `unit`, not measurement.** The SD of a change from
  baseline is not the SD of a raw value, and an FEV1 SD in millilitres is not
  one in litres. Sponsors report FEV1 both ways; `scales.yaml` converts, so a
  standard error of `21 mL` and a standard deviation of `0.281 L` end up in the
  same distribution correctly rather than in the same distribution wrongly.
* **The provenance line is part of the answer.** `reported 5 ·
  from_standard_error 4 · from_confidence_interval 2` says how much of this
  median is an SD a trial actually printed and how much is one this pipeline
  derived. `--only-reported` drops the derivations; `--no-approximate` drops
  the Wan et al. IQR/range estimators, which are approximations rather than
  conversions.
* **The coverage line is the denominator.** Without it the command is a machine
  for producing confident numbers off eight arms.

`--source baseline` answers the adjacent question -- the baseline SD, which is
a different quantity and should never be silently substituted for a change-score
SD:

```
measurement=fev1, source=baseline
  not_stated · litres   studies 4   arms 9   SD median 0.5   IQR 0.48-0.53
```

Half a litre at baseline against ~0.30 L on the change score is the
within-patient correlation showing up as it should, and it is exactly the
distinction that gets lost when someone pulls "the FEV1 SD" out of one paper.

---

## 2. What is the endpoint convention in this indication?

**Who asks.** Whoever is choosing the primary endpoint and then writing its
definition -- clinical development lead, medical writer, the biostatistician
who has to make it estimable. Two separate decisions hide inside "what is
standard here": which endpoint, and, once chosen, at which threshold and which
timepoint. Getting the second wrong is the more expensive mistake, because a
PASI 75 trial and a PASI 90 trial are not comparable and the regulator has
seen both.

**Which endpoint.** Join the conformed endpoints out to therapeutic area and
look at what primaries other sponsors picked:

```sql
SELECT ta.ta_id, e.form_id, e.measurement_id, count(DISTINCT e.nct_id) AS studies
FROM conformed.endpoints e
JOIN conformed.study_therapeutic_area ta ON ta.nct_id = e.nct_id AND ta.is_primary
WHERE e.outcome_type = 'PRIMARY'
GROUP BY 1, 2, 3 HAVING count(DISTINCT e.nct_id) > 1 ORDER BY 1, 4 DESC;
```

```
cardiovascular       time_to_event          heart_failure_hospitalisation   2
dermatology          responder_proportion   physicians_global_assessment_skin  2
dermatology          responder_proportion   pasi                            2
metabolic_endocrine  change_from_baseline   hba1c                           2
oncology             time_to_event          tumour_burden_recist            3
oncology             time_to_event          disease_recurrence              2
respiratory          event_rate             disease_exacerbation            2
respiratory          change_from_baseline   fev1                            2
```

That is the endpoint-selection landscape as a table rather than as a literature
review. `pull --org "<sponsor>"` narrows the same query to one company's
portfolio, which is the competitive-intelligence version of the question.

**At which threshold.** Responder definitions are parsed out of the registry
string into `threshold_comparator` / `threshold_value` / `threshold_unit`, so
the convention is countable:

```sql
SELECT measurement_id, form_id, threshold_comparator, threshold_value, threshold_unit,
       count(DISTINCT nct_id) AS studies,
       count(*) FILTER (WHERE measurement_match_method = 'exact') AS exact_matches
FROM conformed.endpoints WHERE threshold_value IS NOT NULL
GROUP BY 1,2,3,4,5 ORDER BY studies DESC;
```

```
pasi                              responder_proportion  >=  100.0  %       2  2
hba1c                             responder_proportion  <     7.0  %       2  2
st_georges_respiratory_questionn…  responder_proportion  >=   4.0  Points  1  1
st_georges_respiratory_questionn…  responder_proportion  >=   4.0  NULL    1  1
pasi                              responder_proportion  >=   75.0  %       1  1
easi                              responder_proportion  >=   75.0  %       1  1
pasi                              responder_proportion  >=   90.0  %       1  1
```

`exact_matches` is what separates a convention from a one-off: a threshold that
recurs across sponsors, every one an `exact` match, is a standard (HbA1c < 7.0%
is the ADA target). A threshold appearing once behind a `semantic` match is one
team's choice.

The two SGRQ rows are worth reading rather than tidying away. They are the same
published 4-point MCID, written by two sponsors as *"(Decrease of >= 4 Units)"*
and *"a Decrease of at Least 4 Points"*; the parser recovered the comparator and
the value from both and the unit from only one, so they group separately.
Grouping on `threshold_value` rather than on the whole tuple is the fix when
counting conventions -- and the split is a reminder that the threshold parser
reads text, so it inherits the text's inconsistencies.

**At which timepoint.** `timepoint_pattern` classifies the `time_frame` string
into a small set of shapes, and `timepoint_extracted` holds what was pulled out
of it:

```sql
SELECT timepoint_pattern, count(*) FROM conformed.endpoints GROUP BY 1 ORDER BY 2 DESC;
```

```
baseline_to_timepoint  34    single_fixed     15    anchored_offset  1
bare_duration          22    event_driven      1    multi_timepoint  1
cumulative_window      20
```

The practical use is narrower than the totals: filter to one measurement and
the answer is "the assessment visit this endpoint is conventionally read at",
which is a schedule-of-assessments input, not trivia.

---

## 3. Which trials measured the same thing a different way?

**Who asks.** HEOR and evidence-synthesis teams deciding whether an indirect
comparison or a meta-analysis is defensible, and clinical teams asked in a
sponsor meeting why their result "disagrees" with a competitor's. The failure
mode is symmetric: pooling two trials that measured different things, or
treating two trials that measured the same thing as incomparable because the
strings differed.

There are two versions of the question, and they need different joins.

**Same quantity, different kind of number.** `measurement_id` is the quantity;
`form_id` is what kind of number was derived from it.

```sql
SELECT measurement_id, count(DISTINCT form_id) AS forms, count(DISTINCT nct_id) AS studies,
       string_agg(DISTINCT form_id, ', ') AS forms_seen
FROM conformed.endpoints GROUP BY 1
HAVING count(DISTINCT form_id) > 1 AND count(DISTINCT nct_id) > 1
ORDER BY forms DESC, studies DESC;
```

```
pasi          3  3  responder_proportion, change_from_baseline, percent_change_from_baseline
vital_status  2 10  incidence_proportion, time_to_event
fev1          2  5  not_stated, change_from_baseline
dlqi          2  4  change_from_baseline, not_stated
tumour_burden_recist  2  4  time_to_event, responder_proportion
```

`vital_status` across ten studies is the textbook case: the same measurement,
reported as a survival distribution in seven trials and as a mortality
proportion in three -- and `direction_id` correctly reverses between them,
because direction is *derived* from form plus measurement polarity rather than
matched from the text:

```
vital_status  time_to_event          longer_is_better    7 studies
vital_status  incidence_proportion   decrease_is_better  3 studies
```

Nothing in either registry string says which way is better. Getting this wrong
is how a forest plot ends up with a sign error.

**Same concept, different instrument.** The harder case, which the form join
cannot reach: two trials both measured respiratory quality of life, one with
SGRQ and one with AQLQ. `measurements.yaml` names instruments at instrument
level -- deliberately, since SGRQ and AQLQ are not interchangeable -- and
carries a `concept` for exactly this query.

```sql
SELECT m.concept, count(DISTINCT e.measurement_id) AS instruments,
       count(DISTINCT e.nct_id) AS studies, string_agg(DISTINCT e.measurement_id, ', ')
FROM conformed.endpoints e JOIN vocab.measurements m ON m.id = e.measurement_id
WHERE m.concept IS NOT NULL GROUP BY 1
HAVING count(DISTINCT e.measurement_id) > 1 ORDER BY 2 DESC, 3 DESC;
```

```
cardiovascular_event          3  4  heart_failure_hospitalisation, major_adverse_cardiovascular_event, stent_thrombosis
respiratory_quality_of_life   2  3  asthma_quality_of_life_questionnaire, st_georges_respiratory_questionnaire
health_related_quality_of_life 2 3  eortc_qlq_c30, fact_g
blood_pressure                2  2  systolic_blood_pressure, diastolic_blood_pressure
```

Each row is a candidate mapping exercise, not a completed one. The answer is
"these trials are in scope for a comparison and here is the instrument
mismatch you would have to defend" -- which is the useful answer, because the
alternative is discovering the mismatch in peer review.

Note what the vocabulary deliberately does *not* collapse: PASI sits under
`psoriasis_severity` and sPGA under `skin_disease_global_severity`, so the
query above will not offer them as one concept. Both are psoriasis severity in
the loose sense, and neither trial would accept the other's as its endpoint;
where that boundary falls is a judgment call, and `vocab/README.md` records the
reasoning behind each one.

---

## 4. What non-inferiority margin has precedent here?

**Who asks.** Regulatory strategy and biostatistics, designing a
non-inferiority trial. The margin is the single most negotiable -- and most
challengeable -- number in the protocol, and the defence for it is precedent:
what margin has been accepted for this endpoint before, in trials of this size.
That precedent is buried in free text in the analysis section of the registry.

```bash
uv run endpoints stats --measurement hba1c --analyses
```

```
  effect measures
effect           unit        studies  analyses  median   IQR              null
mean_difference  proportion  2        2         -0.0111  -0.0117--0.0105  0

  p-values
    stated 2 · exact 0 · censored 2 · below 0.05 2 (100.0% of stated)
    a censored p-value ('<0.001') contributes its bound, not an observed value

  non-inferiority
study        effect                   margin  from
NCT90000033  Mean Difference (Final   0.3 %   Non-inferiority margin of 0.3%
             Values)                          for the change from baseline

  coverage    2 of 2 conformed studies reported at least one analysis (100.0%)
```

The margin is parsed out of the sponsor's own prose where it can be, and the
prose is shown where it cannot -- so the row is always traceable to the trial
that used it rather than to a parser's guess. Same for p-values: a censored
`<0.001` contributes its bound and is counted separately from an exact value,
because averaging censored and exact p-values together is how a "median
p-value" becomes meaningless.

Every margin in the corpus, with its trials, is also one join away:

```sql
SELECT r.nct_id, a.param_type, a.non_inferiority_type, a.non_inferiority_description
FROM raw.outcome_analyses a
JOIN conformed.endpoint_results r ON r.source_id = a.outcome_id AND r.result_kind = 'outcome'
WHERE a.non_inferiority AND r.measurement_id = 'hba1c';
```

---

## 5. Did trials report the endpoints they registered?

**Who asks.** Clinical operations and medical writing running a QC pass before
results disclosure; systematic reviewers and journal editors assessing outcome
reporting bias. The question is a two-sided join between what a study
registered and what it later posted, and it is normally done by eye, one trial
at a time.

`results conform` runs the reported outcome titles through the *same*
conformance engine the protocol side uses, then records how each reported
outcome was linked to a planned one, with its own provenance:

| `link_method` | what it means |
|---|---|
| `exact_title` | the reported title is, after normalisation, a planned `measure` in that study |
| `conformed_measurement` | different strings, same conformed measurement in that study |
| NULL | no planned counterpart -- kept, flagged, never force-joined |

Three findings fall out of it.

**Reported but never registered** lands in the results review queue rather than
being attached to whichever planned endpoint was closest:

```sql
SELECT nct_id, title_raw, measurement_id FROM conformed.results_review_queue
WHERE reason = 'unlinked_to_planned';
```

```
NCT90000010  Change From Baseline in COPD Assessment Test Score at Week 52  copd_assessment_test
```

**Registered but never reported** is the same join from the other side:

```sql
SELECT e.nct_id, e.outcome_type, e.measure_raw, e.measurement_id, e.form_id
FROM conformed.endpoints e JOIN raw.studies s ON s.nct_id = e.nct_id
WHERE s.has_results AND e.outcome_type = 'PRIMARY'
  AND e.endpoint_id NOT IN (SELECT planned_endpoint_id FROM conformed.endpoint_results
                            WHERE planned_endpoint_id IS NOT NULL);
```

**Reported in a different form than registered** is the subtlest of the three,
and the one hardest to catch by reading. `link_agrees_on_form` compares the
form conformed from the reported title against the form conformed from the
planned measure:

```sql
SELECT r.nct_id, e.measure_raw AS planned, e.form_id AS planned_form,
       r.measure_raw AS reported, r.form_id AS reported_form, r.link_method
FROM conformed.endpoint_results r
JOIN conformed.endpoints e ON e.endpoint_id = r.planned_endpoint_id
WHERE r.link_agrees_on_form = false;
```

```
NCT90000035
  planned   Change From Baseline in Mean Seated Systolic Blood Pressure at Week 12
            -> change_from_baseline
  reported  Percentage of Participants Achieving a Seated Systolic Blood Pressure
            Below 140 mmHg at Week 12  -> responder_proportion
  linked via conformed_measurement
```

Same study, same measurement, a continuous endpoint registered and a
dichotomised one reported. Whether that is a protocol amendment, a
pre-specified secondary analysis or a finding is a human judgment -- the point
is that the row surfaces instead of matching silently on the shared
measurement.

`endpoints results coverage` puts the same question at corpus level, including
the honest denominators:

```
1. Results posting
  21 of 24 pulled studies are flagged hasResults (87.5%)
  18 had a results section landed (75.0% of pulled)
2. Reported titles vs planned measures
  exact_title  25  (100.0%)
```

---

## 6. Can our endpoint text leave the registry as CDISC USDM 4.0?

**Who asks.** Data standards, and whoever is building the study in a system
that speaks USDM -- a protocol authoring tool, an EDC spec, a downstream
metadata repository. Registry endpoint text is prose; USDM wants structure.
The default is that somebody retypes it, and the retyping is where the
definition quietly changes.

```bash
uv run endpoints usdm show <NCT_ID> --level primary
uv run endpoints serve --port 8000   # GET /v4/studies/{nctId}/endpoints
```

The projection's design decision is what makes it auditable: each endpoint's
`text` is a *syntax template* whose tags resolve, through that endpoint's own
`SyntaxTemplateDictionary`, back into the controlled vocabularies -- while the
original registry string is kept verbatim in `description`.

```
Percentage of Participants Achieving PASI 100 at Week 16
  -> <p>Proportion of participants achieving <usdm:tag name="threshold"/> in
        <usdm:tag name="measurement"/> <usdm:tag name="timepoint"/></p>

Change From Baseline in Dermatology Life Quality Index (DLQI) at Week 16
  -> <p>Change from <usdm:tag name="reference"/> in <usdm:tag name="measurement"/>
        <usdm:tag name="timepoint"/> (<usdm:tag name="scale"/>)</p>
```

So the output is a projection, not a rewrite: a reviewer can see both the
structure and the string it came from, and every synthesized or defaulted
attribute is flagged as such rather than presented as something the sponsor
wrote.

The other half of the answer is that endpoint *counts* are preserved. Every raw
outcome row becomes exactly one USDM `Endpoint`, at one of three fidelity tiers,
so a study whose endpoints did not conform renders less richly but never appears
to have fewer endpoints:

```
uv run endpoints usdm coverage

tier       endpoints  share
templated         71  72.4%
partial           17  17.3%
verbatim          10  10.2%
total             98
reference defaulted: 2 (2.8% of templated)
```

`partial` and `verbatim` are the honest tiers. A study sitting at `verbatim`
tells you its endpoints need a human before anything downstream consumes them,
which is more useful than a uniformly confident payload.

---

## What this cannot tell you

Every answer above is drawn from rows that conformed, and that is a biased
subset in a specific direction. Three limits are worth carrying into any use of
this page:

* **Measurement coverage is partial and head-weighted.** A row whose
  measurement resolves nowhere goes to `conformed.review_queue` and never
  becomes comparable to anything. Round-two coverage was 64.5% on an unbiased
  sample, and 77% of `measure` strings occur exactly once -- so an unusual
  instrument used in two trials is disproportionately likely to be missing.
  Absence of a link is not evidence that no link exists; check the review queue
  before concluding anything from an empty result.
* **The variability answers stack three biases.** Not every trial posts results;
  not every endpoint conforms; not every posted result carries a usable
  dispersion. `endpoints stats` prints the third denominator on every group,
  and `endpoints results coverage` prints the first two.
* **Population is not a structured axis.** Neither side of the warehouse models
  it, so an SD pooled across a severe-disease enrichment population and a broad
  one is pooled across a real clinical difference, invisibly. For question 1
  especially, read `population` on the contributing rows before using a median.

[`QUERY_CHEATSHEET.md`](QUERY_CHEATSHEET.md) carries the same caveats next to
the queries they apply to.

## How these were validated

Every command and query on this page was executed, end to end, through the real
pipeline -- `vocab validate` -> TA resolution -> `conform` -> `results conform`
-> `stats` / `usdm` -- and the outputs above are pasted from those runs. The
full test suite (520 passed, 1 skipped) was run alongside.

One caveat matters for how you read the numbers. The environment this was
validated in cannot reach ClinicalTrials.gov, so `endpoints pull` could not
run. In its place, a 24-study corpus was seeded directly into `raw.*` through
the project's own DDL -- Phase 2/3 registrations across oncology, respiratory,
dermatology, cardiovascular/metabolic and neurology, with endpoint text written
in registry idiom and a posted results section for 18 of them.

Question 5 needed one more step: the seeded trials all reported what they
registered, so the two drift paths had nothing to fire on. A copy of the
warehouse gained two extra reported outcomes -- one never registered, one
registered as a change from baseline and reported as a responder proportion --
and `results conform` was re-run over it. That is what the `unlinked_to_planned`
and `link_agrees_on_form = false` examples above come from: the detection is
real, the two trials behind those rows are constructions.

So: **the mechanisms are validated, the numbers are not findings about the
registry.** "`endpoints stats --measurement fev1` pools millilitre-reported and
litre-reported trials into one SD distribution and shows its provenance" is
established. "The FEV1 change-from-baseline SD is 0.30 L" is not -- that is a
property of 24 seeded studies. Re-run each command after a real `pull` to get
answers about the actual corpus.

For the record, what the seeded run produced: 100 registered outcomes, 94
conformed and 6 queued; 45 reported results rows conformed with 62 of 74
arm-level measurements yielding an SD estimate; 24 studies resolved across six
therapeutic areas.
