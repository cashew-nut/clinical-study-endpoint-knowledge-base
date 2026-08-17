# Annotation guideline

This is the instruction set for producing a gold standard: a set of human judgements
about what registry outcomes actually are, against which the classifier can be scored.

It exists because **coverage is not accuracy**. Coverage says how many outcomes matched
something. Only annotation says how many matched the *right* thing, and the two numbers
can diverge without limit — a rule that matched everything to `AE_INCIDENCE` would report
100% coverage.

---

## The order of work

1. **Two annotators, independently.** Not one.
2. **Measure agreement first** — `ceskb agreement a.yaml b.yaml`.
3. **Adjudicate disagreements**, and treat each one as a question about the *vocabulary*
   before treating it as a question about the item.
4. **Only then** score the classifier — `ceskb evaluate`.

Step 2 is not a formality. If two people applying this guideline disagree about what an
outcome is, that disagreement is not the classifier's fault and cannot be tuned away.
It means the concept definitions or this guideline are underspecified, and the fix is
upstream. A classifier scored against a gold set that annotators cannot reproduce is
measuring noise.

Rough reading of Cohen's kappa: below 0.6, fix the definitions before going further;
0.6–0.8 is usable with adjudication; above 0.8 the task is well posed.

---

## What you are deciding

**One concept per outcome**, from `vocabularies/concepts/`, or explicitly *nothing*.

Optionally, **specific axis values** where the text settles them. You are not obliged to
judge every axis — only the ones you annotate are scored. Annotating three axes you are
sure of beats annotating eleven you are guessing at.

### Use only the text

Judge from `measure`, `description` and `time_frame` as the registry states them. Do not
open the protocol, the publication, or the classifier's output. The classifier sees only
this text, so scoring it against knowledge the text does not contain measures the wrong
thing.

If the text is insufficient, that *is* the finding: annotate `concept_id: null` and mark
`difficulty: ambiguous`.

### Null is a real answer

`concept_id: null` means "no concept in the vocabulary describes this outcome". It is a
judgement, not a skip, and it is essential: without null items a classifier can score
perfectly by matching everything to something. A gold set with no null items is not
measuring precision.

Reserve it for genuine cases — "Investigator-Assessed Clinical Benefit, not otherwise
specified" — rather than for outcomes you find hard.

---

## How to choose a concept

### Match on what is measured and how it is derived, not on wording

`"Percentage of Participants With Complete Response or Partial Response"` never says
"ORR", but CR-or-PR *is* the definition of objective response rate. Annotate `ORR`.
Conversely `"Complete Response Rate"` is `CR_RATE` even though a CR is also an objective
response.

### Form is a parameter, not a new concept

The same measurement analysed differently stays the same concept and changes
`endpoint_form`:

| Text | Concept | `endpoint_form` |
|---|---|---|
| Progression-Free Survival | `PFS` | `time_to_event` |
| Progression-Free Survival Rate at 12 Months | `PFS` | `responder_binary` |
| Annualized Rate of Exacerbations | `EXACERBATION_RATE` | `event_rate` |
| Number of Exacerbations | `EXACERBATION_RATE` | `count` |

A landmark rate is not a separate concept from the time-to-event endpoint it is derived
from. This follows the project's granularity rule: **fewer concepts, more parameters.**

### Thresholds are parameters too

"at least 5% weight loss" and "at least 10% weight loss" are one concept
(`WEIGHT_LOSS_RESPONDER`) at two threshold values. ACR20 and ACR50 are the exception:
they are separate concepts because each names a distinct, separately-validated composite
criteria set, not one criterion at two cut-points.

### Percent change is its own form

If the text says "Percent Change From Baseline", the form is
`percent_change_from_baseline`, not `change_from_baseline`. The vocabulary distinguishes
them; conflating them is the single most common annotation slip.

---

## Axes that reward care

### `reference_type` — what the *endpoint* compares against

Distinguish the reference built into the measurement from the reference the endpoint
uses. "Absolute Change From Baseline in Percent Predicted FEV1" contains both: *percent
predicted* references the measurement to a population norm, but the endpoint compares
the participant to their own earlier value. The axis describes the endpoint, so it is
`patient_baseline`.

Similarly, "Percentage of Participants Achieving HbA1c < 7.0%" is
`fixed_clinical_target` — an absolute cut-point — not `patient_baseline`, because
nothing is compared to the participant's own starting value.

### `direction` — check the instrument, not the concept

Most quality-of-life instruments score higher as better. Symptom-burden instruments
invert this: SGRQ and the COPD Assessment Test score higher for *worse* health, so a
fall is improvement. KCCQ, FACT and EORTC QLQ functional scales do not invert.

Getting this wrong reports the sign of the treatment effect backwards, and it is the kind
of error that survives review because the concept and the number both look right on
their own. Annotate `direction` whenever a named instrument is involved.

### `analysis_population`

Annotate only when the text states it. "Full analysis set", "intent-to-treat",
"safety population" appear in descriptions often enough to be worth capturing; absence
is `unspecified` and should be left unannotated rather than guessed.

---

## Recording difficulty

- `clear` — the text names a standard endpoint and one concept plainly fits.
- `judgement` — a defensible alternative reading exists; you chose one and said why.
- `ambiguous` — the text does not settle it.

These are reported separately. Accuracy on `clear` items and accuracy on `ambiguous`
items mean different things, and a headline figure that mixes them is less informative
than either.

Write a `note` for every `judgement` and `ambiguous` item. The note is what an
adjudicator reads.

---

## File format

```yaml
gold_set_id: phase3-sample-a
version: "1"
annotator: your name
annotated_at: 2026-08-17
guideline: docs/ANNOTATION.md
independence: independent      # self_annotated | independent | adjudicated
items:
  - outcome_uid: NCT01234567:primary:0
    concept_id: PFS
    text: "Progression-Free Survival per RECIST v1.1"
    source_hash: 5b34a308f861…
    axes:
      endpoint_form: time_to_event
      reference_type: randomisation_time
    difficulty: clear
    note: ""
```

`source_hash` binds the annotation to the exact text you read. If the registry rewrites
that outcome, the hash stops matching and the item is **excluded from scoring** rather
than graded against words you never saw. Fill it from
`SELECT record_hash FROM study_outcome`.

`independence` is required and is not decoration. `self_annotated` means the same party
authored the rules and the answers, so any shared misconception is invisible; the scorer
prints a caveat and the resulting figures are regression detection, not evidence of
accuracy. Only `independent` and `adjudicated` sets support a claim about quality.

Validate with `ceskb validate`, which cross-checks every concept and term id against the
vocabulary.

---

## Scoring, and why the gate is on precision

```bash
ceskb evaluate --min-precision 0.95 --max-excluded 0
```

The gate is set per concept rather than on the average, because one concept mapping badly
is a real defect and averaging hides it behind forty that map well.

It is set on **precision** rather than recall because the two errors are not symmetric.
An unmatched outcome is visibly absent — it sits in the Gaps view with its text, waiting
for a rule. A *wrongly* matched outcome is invisible: it silently joins a prevalence
count, a cross-study comparison, a USDM document. Recall gaps announce themselves;
precision failures do not. Let coverage lag.

`--max-excluded` guards the denominator. A score computed over a shrinking scored set
while the excluded pile grows is exactly the kind of number that quietly stops meaning
anything.
