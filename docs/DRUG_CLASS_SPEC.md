# Design spec: grouping endpoints by drug class

> **Status: not implemented.** Nothing here exists in code: there is no
> `vocab/drug_classes.yaml`, no `raw.interventions`, no
> `conformed.study_drug_class`, no `--drug-class` on any command. This is a
> proposal to schedule or decide against.
>
> The short answer to the question that prompted it — *can this be grouped by
> drug class from ClinicalTrials.gov data?* — is **yes, at two granularities,
> from fields already inside the payload `pull` fetches today**, and **no** at
> ATC granularity without an external vocabulary this project does not have.
> See [What the registry actually gives you](#what-the-registry-actually-gives-you).
>
> Phase 0 is a measurement, not code, and it needs a live pull — the same gate
> [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md#the-gate) and
> [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md#still-owed)
> are both stuck behind. See [Phasing, with a gate](#phasing-with-a-gate).

Read alongside:

* [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md) —
  the therapeutic-area axis, which this deliberately copies rather than reinvents
* [`../vocab/ta_mesh_mapping.yaml`](../vocab/ta_mesh_mapping.yaml) — the layered
  MeSH mapping this parallels, and its `caveats` block
* [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) — `endpoints stats`,
  which is where a drug-class axis pays for itself and also where it can most
  easily produce a confidently wrong number

---

## The short answer

| granularity | example | available? | from what |
|---|---|---|---|
| **modality** | drug / biologic / device / behavioural / procedure | **yes, exactly** | `armsInterventionsModule.interventions[].type`, AACT `ctgov.interventions.intervention_type` — a registry field, not an inference |
| **coarse pharmacologic class** | antineoplastic agents, cardiovascular agents, anti-infectives | **yes** | `derivedSection.interventionBrowseModule.browseBranches[]` — NLM-assigned, one hop from MeSH pharmacologic-action categories |
| **mechanism class** | PD-1 checkpoint inhibitor, GLP-1 receptor agonist, SGLT2 inhibitor | **yes, curated** | MeSH intervention descriptors + `ancestors[]`, mapped through a vocabulary this project writes, exactly as therapeutic area is today |
| **ATC class** | `L01FF02`, `A10BJ` | **no** | ClinicalTrials.gov carries no ATC code, on either backend. Needs RxNorm/ATC — an external vocabulary, a network dependency, and a licensing question |
| **specific agent** | pembrolizumab | **yes**, as a string; **partly**, as a controlled id | MeSH descriptor when NLM assigned one; otherwise the sponsor's free-text `interventions[].name` plus `otherNames[]` |

The middle row is the interesting one and the one this spec is about. It is the
same problem shape as therapeutic area — *a study attribute nobody publishes,
derivable from NLM's MeSH coding through a layered mapping this project owns* —
and it should be built the same way, or not at all.

## What the registry actually gives you

Four sources. Two are already landed and half-discarded; two are not landed at all.

| source | CT.gov API v2 | AACT | landed today? |
|---|---|---|---|
| MeSH intervention descriptors | `derivedSection.interventionBrowseModule.meshes[]` | `ctgov.browse_interventions` | **yes** → `raw.browse_interventions` |
| MeSH ancestors of those descriptors | `interventionBrowseModule.ancestors[]` | not exposed as such | **no** — `_extract_mesh_rows` reads only `meshes[]` |
| coarse pharmacologic branches | `interventionBrowseModule.browseBranches[]` | no equivalent | **no** — `_extract_condition_branch_rows` is hardcoded to `conditionBrowseModule` |
| the interventions themselves | `protocolSection.armsInterventionsModule.interventions[]` | `ctgov.interventions`, `ctgov.intervention_other_names`, `ctgov.design_group_interventions` | **no** — `pull` lands `armGroups[]` as `raw.design_groups` and drops `interventions[]` on the floor |

Three consequences worth stating plainly, because they change the cost estimate:

**Most of this costs no network.** The CT.gov backend already fetches the whole
study record — `_fetch_page` sends no `fields` parameter — so `ancestors[]`,
`browseBranches[]` and `interventions[]` are in a payload the pull has in hand
and throws away. This is the same discovery the results tier made
([`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md), "Landing is on by
default"): the cost is warehouse size and extraction code, not request volume.

**The AACT side needs three new tables, and one of them is the good one.**
`ctgov.design_group_interventions` is a real join table between arms and
interventions — the arm-level link the API backend can only approximate by
string-matching `interventions[].armGroupLabels[]` against `armGroups[].label`.
Per the convention `_pull_mesh_terms` set, every column name here is
introspected at pull time rather than assumed, and a missing table costs the
drug-class axis rather than the pull.

**`raw.browse_interventions.mesh_type` is currently a constant.** Both backends
write the literal `'intervention'` into it (`ingest/aact.py`,
`ingest/ctgov_api.py`). If NLM's assignment list and its ancestor closure are
ever landed in the same table, that column has to start carrying the
distinction — `descriptor` vs `ancestor` — or the two get pooled, and pooling
them silently converts "this trial studies pembrolizumab" into "this trial
studies antineoplastic agents" with no way to tell which was asserted. This
spec lands ancestors in a **separate table** instead, so the existing column's
meaning does not change under any query already written against it.

### What is not available at all

* **ATC codes.** Not in either backend, at any level. A trial of semaglutide
  carries `Semaglutide` (MeSH), not `A10BJ06`. Mapping to ATC means RxNorm or
  the WHO ATC index: an external vocabulary, an egress dependency this project
  has spent two specs designing around, and a licence question. Out of scope —
  and if it is ever wanted, it belongs behind the same curated vocabulary this
  spec proposes, as an *attribute of a class term*, not as a second resolver.
* **Dose, route, schedule.** Free text in `interventions[].description` where
  stated at all. Not modellable at this cost.
* **Which arm "is" the experimental one**, as an assertion. `armGroups[].type`
  (`EXPERIMENTAL`, `ACTIVE_COMPARATOR`, `PLACEBO_COMPARATOR`, `SHAM_COMPARATOR`,
  `NO_INTERVENTION`, `OTHER`) is landed today as `raw.design_groups.group_type`
  and is the closest thing. It is a registry field and mostly reliable, but
  `OTHER` is common and multi-arm platform trials abuse it. Treat it as
  evidence, not as truth — see [Class is an arm property](#class-is-an-arm-property-not-a-study-property).

## The modelling decision: class means mechanism, not chemistry

This is the decision everything else depends on, and getting it wrong produces
groupings that are arithmetically fine and clinically useless.

MeSH classifies a substance **twice**: structurally, by what it is made of (the
D tree — metformin is a *biguanide*), and functionally, by what it does
(pharmacologic actions — metformin is a *hypoglycemic agent*). A flat "walk the
MeSH ancestors and call them classes" implementation mixes both into one column,
and then `GROUP BY drug_class` puts a sulfonylurea and a biguanide in different
groups while putting two unrelated biguanides in the same one.

For grouping *endpoints*, mechanism is the axis that carries signal: two drugs
that lower HbA1c through the same receptor should be expected to behave alike on
an HbA1c endpoint; two drugs that share a benzene ring should not. So:

* `drug_classes.yaml` terms carry a mandatory `kind`, one of
  **`mechanism`** (GLP-1 receptor agonist, PD-1 inhibitor, SGLT2 inhibitor),
  **`pharmacologic`** (antineoplastic agent, antihypertensive — the coarse
  action level CT.gov's browse branches give directly),
  **`modality`** (small molecule, monoclonal antibody, cell therapy, vaccine,
  device, behavioural), or
  **`control`** (placebo, standard of care, no intervention).
* `kind` is mandatory precisely so that every query has to say which axis it
  means, the same way `COMPOSITE_ENDPOINTS_SPEC.md` makes composite `kind`
  mandatory so a comparability query cannot silently pool an event union with a
  scored index.
* **Structural class is not modelled.** Not "modelled badly" — absent. If a use
  for it appears, it is a fourth `kind`, not a redefinition of the other three.

`control` being a first-class kind is not bookkeeping. Half the arms in a
placebo-controlled corpus are placebo arms, and a drug-class axis that cannot
name them cannot exclude them, which means the first honest question anyone asks
of this axis — *what does this class do relative to placebo?* — is unanswerable.

## Class is an arm property, not a study property

A study does not have a drug class. Its **arms** do, and they differ from each
other by construction — that is what a controlled trial is.

Two tiers, with the tier recorded on every row:

| tier | what it asserts | needs |
|---|---|---|
| **study** | this study's interventions include class X | `raw.browse_interventions` + `raw.interventions` — always available |
| **arm** | *this arm* received class X | the arm↔intervention link: AACT's `design_group_interventions`, or the API's `armGroupLabels[]` matched to `armGroups[].label` |

The study tier is cheap, always available, and answers "which trials involve a
GLP-1 agonist". The arm tier is what `endpoints stats` actually needs, because a
dispersion row is per arm: attaching the study's class set to a placebo arm's SD
is not a rounding error, it is a wrong number with a confident denominator.

**The arm tier degrades, it does not guess.** On the API backend the link is a
string match between `interventions[].armGroupLabels[]` and `armGroups[].label`;
where a label does not match exactly (after the same normalisation the
conformance engine already uses), the arm gets no class rather than the study's.
`link_method` on `conformed.arm_drug_class` records which path produced the row —
`join_table` (AACT), `arm_label` (API, exact after normalisation), or nothing at
all — so a query can demand the strong one.

There is a second, worse fragility downstream: the results section's
`outcome_groups.group_key` is the *results* group id, not a protocol arm id, and
the two are linked only by title. That join is out of this spec's scope; until
someone measures how often the titles agree, `stats --drug-class` should be
specified against the **study** tier, with the arm tier feeding
`conformed.arm_drug_class` and waiting for that measurement. Anything else
invents an arm-level number out of a title match nobody has checked.

## Schema

Deliberately shaped so every piece is optional and additive: nothing here
changes an existing table's meaning, and every table can be absent without
breaking a command that does not ask for it.

### New raw tables

```
raw.interventions                  nct_id, ordinal, intervention_type, name,
                                   name_normalised, description
raw.intervention_other_names       nct_id, ordinal, other_name, other_name_normalised
raw.arm_interventions              nct_id, group_title, intervention_ordinal, link_method
raw.browse_intervention_ancestors  nct_id, mesh_term, mesh_term_normalised, descendant_term
raw.browse_intervention_branches   nct_id, branch_abbrev, branch_name
```

`name_normalised` is computed here, by the same `normalise` the vocabulary
loader uses, rather than trusted from a source column — the precedent
`ingest/aact.py` set when it declined to trust an unverified
`downcase_mesh_term`.

`raw.browse_intervention_branches` mirrors `raw.browse_condition_branches`
exactly, including its unverified-abbreviation caveat. The condition branches
turned out to be `B` + a MeSH tree code (`BC04` = Neoplasms); the **intervention
branch abbreviations are not that shape** and are not documented anywhere this
project can reach. Phase 0 has to observe them, not guess them — see the gate.

### New vocabulary files

Two, matching the split therapeutic area already uses (a term list, and a
mapping that is not a term list):

* **`vocab/drug_classes.yaml`** — a new `DimensionSpec` in `vocab/schema.py`:
  columns `id, label, inline_label, definition, kind, parent, precedence,
  notes`, with `parent` a self-reference validated for cycles. One shallow
  level of nesting (`pd1_inhibitor` → `checkpoint_inhibitor` →
  `antineoplastic_immunological`) is enough; this is not the recursive
  structure `COMPOSITE_ENDPOINTS_SPEC.md` argues for, and should not grow into
  one.
* **`vocab/drug_class_mesh_mapping.yaml`** — loaded separately, like
  `ta_mesh_mapping.yaml`, holding the layers below.

`inline_label` exists so a class can appear in a USDM syntax template later
without a second naming decision. Nothing in this spec renders one.

### New conformed tables

```
conformed.study_drug_class   nct_id, drug_class_id, kind, rule_layer, matched_on,
                             is_primary
conformed.arm_drug_class     nct_id, group_title, drug_class_id, kind, rule_layer,
                             matched_on, link_method
conformed.drug_class_review_queue
                             nct_id, ordinal, name, mesh_term, reason
```

`conformed.study_drug_class` is column-for-column the shape of
`conformed.study_therapeutic_area` plus `kind`, on purpose: a query written
against one works against the other with a table name changed.

## The resolver

`src/clinical_endpoints/drug_class/resolver.py`, structured as
`ta/resolver.py` is, including the split that lets pull-time filtering and bulk
resolution share one function (`resolve_study_drug_class_matches`) so a
`--drug-class` pull can never disagree with the table written afterwards.

Layers, first hit wins **per intervention**, every layer's matches kept:

| # | layer | against | why it is where it is |
|---|---|---|---|
| 0 | `control_rules` | `raw.interventions.name` + `intervention_type` | placebo/sham/SOC first, so a placebo arm is never classed by a MeSH term the sponsor attached to the study as a whole |
| 1 | `term_overrides` | MeSH descriptor, exact | where multi-class ambiguity is settled by hand |
| 2 | `agent_names` | `name_normalised` + `other_name_normalised` | the sponsor's own drug name and its aliases, for agents NLM has not coded (new molecular entities are routinely uncoded for a year or more) |
| 3 | `ancestor_rules` | `raw.browse_intervention_ancestors` | NLM's own hierarchy — the mechanism signal, where it exists |
| 4 | `branch_rules` | `raw.browse_intervention_branches` | the coarse pharmacologic level; always `kind: pharmacologic`, never a mechanism claim |
| 5 | `modality_rules` | `intervention_type` | the registry field; always fires, so every intervention gets at least a modality |
| 6 | `defaults` | — | `unclassified_agent` (an intervention with no match) / `no_interventions_stated` |

Layer 2 sitting **above** the MeSH layers is the one ordering choice that is not
copied from the TA resolver, and it is deliberate: a curated agent→class entry
is a human assertion about a specific drug, while an ancestor match is an
inference from NLM's coding of it. Where they disagree, the human wins, and the
disagreement is worth surfacing — see `drug-class diff-ancestors` below.

**Keep all matches; precedence picks a primary.** Combination therapy is not an
edge case (it is standard of care in oncology and increasingly in diabetes), so
a trial of pembrolizumab plus chemotherapy is a checkpoint-inhibitor trial *and*
a cytotoxic-chemotherapy trial. Collapsing to one would be the same mistake
`therapeutic_areas.yaml` explicitly refuses for a lung-cancer trial.

**Unclassified is visible, never silent.** An intervention that reaches layer 6
lands in `conformed.drug_class_review_queue` as well as taking the default, so
the coverage number and the work queue are the same list — the property
`conformed.review_queue` already has on the endpoint side.

## CLI surface

```bash
endpoints pull --phase 3 --drug-class glp1_receptor_agonist   # client-side, before --limit
endpoints drug-class distribution                             # what the corpus is made of
endpoints drug-class diff-ancestors --out drug_class_diff.csv # curated vs NLM disagreements
endpoints stats --measurement hba1c --drug-class glp1_receptor_agonist
endpoints stats --measurement hba1c --by drug-class           # stratify instead of filter
```

`--drug-class` on `pull` is a client-side filter applied *before* `--limit`, for
exactly the reason `--ta` is (`MAX_PAGES_TA_FILTERED`): neither backend can
express it server-side, and filtering after truncation starves a narrow class of
matches it actually has. It should reuse `--ta`'s scan-cap machinery rather than
grow its own.

`diff-ancestors` is the counterpart of `endpoints ta diff-tree`, and has the
same discipline: it **reports disagreements between the curated layer and NLM's
ancestry, it does not reconcile them**. Editing the YAML until the diff is empty
destroys the only external check this axis has.

## Where a drug-class axis changes what a number means

`endpoints stats` is where this earns its place, and also where it can do the
most damage, so the interaction needs stating rather than leaving to the reader.

**On dispersion (the default `stats` output), drug class is a diagnostic, not a
grouping key.** The SD of change-from-baseline HbA1c is mostly a property of the
population, the assay and the timepoint — not of the drug. Splitting an already
thin SD library by drug class mostly buys smaller denominators. Its real use is
the opposite direction: if the SD *does* differ sharply by class, that is
evidence the groups are not exchangeable and the pooled number was already
wrong. Hence `--by drug-class` as a stratifier alongside `--drug-class` as a
filter.

**On `--analyses` (effect sizes), drug class is load-bearing.** Pooling
treatment effects across mechanisms is not a variance question, it is a category
error: the median effect on HbA1c across "all drugs" is a number with no
referent. Any effect-size distribution this project reports should be
class-aware or explicitly say it is not.

**Coverage stays attached.** Every rule in
[`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) applies unchanged: a
class-filtered group ships its denominator, and `unclassified_agent` is never
pooled into a named class to make one look better populated.

## What this makes answerable

None of these are answerable today, at any coverage.

1. **"What SD should I assume for HbA1c at 26 weeks in GLP-1 trials?"** — the
   question `stats` exists for, narrowed to the comparator set a protocol
   author actually has.
2. **"Do checkpoint-inhibitor trials use different endpoints from
   chemotherapy trials in the same tumour type?"** — a cross-tab of
   `measurement_id` × `drug_class_id` within one TA. Endpoint *choice* is a
   class-level convention, and this is the first way to see it.
3. **"Which classes have adopted PFS over OS, and when?"** — the same cross-tab
   with `start_date`, which is the regulatory-history question this corpus is
   uniquely shaped to answer.
4. **"Placebo-arm variability for this endpoint"** — `kind: control` plus the
   arm tier, which is the single most useful quantity in a sample-size
   calculation and the one most often reconstructed by hand.
5. **"Is my endpoint conventional for this class?"** — the frequency of a
   measurement within a class, versus outside it.

## Phasing, with a gate

**Phase 0 — measure, before writing any vocabulary.** One live pull of ~500
Phase 3 studies, then four counts:

| # | question | why it gates the rest |
|---|---|---|
| 1 | Does `interventionBrowseModule` carry `ancestors[]`, and what is in it? | Layer 3 is the mechanism signal. If ancestors are absent or purely structural, mechanism classes have to come from the curated `agent_names` layer alone, and the vocabulary is a much bigger hand-written object |
| 2 | What are the intervention `browseBranches[]` abbreviations? | `BC04`-style parsing is condition-specific. Layer 4 cannot be written against a guess |
| 3 | What share of studies have ≥1 MeSH-coded intervention at all? | The ceiling on every MeSH-derived layer. `raw.browse_interventions` is already landed, so this one is answerable from an existing pull |
| 4 | Does the arm↔intervention link resolve, and how often? | Decides whether the arm tier is built now or deferred. AACT: does `design_group_interventions` exist? API: what share of `armGroupLabels[]` match an `armGroups[].label` exactly after normalisation? |

Question 3 is answerable the moment any warehouse with a real pull exists.
Questions 1, 2 and 4 need a pull from an environment with egress — **the same
blocker three other specs record, and this proposal should not be scheduled
ahead of resolving it.** A vocabulary written against guessed field shapes is
the failure mode `ta_mesh_mapping.yaml`'s caveats block exists to prevent.

**Phase 1 — land it.** The five raw tables, both backends, introspected not
assumed, extraction-only. Independently useful: `raw.interventions` alone makes
"which trials studied drug X" a query, with no vocabulary at all.

**Phase 2 — the vocabulary and the study tier.** `drug_classes.yaml`,
`drug_class_mesh_mapping.yaml`, the resolver, `conformed.study_drug_class`,
`conformed.drug_class_review_queue`. Scope the first vocabulary round to the
classes the corpus is actually made of (Phase 0 question 3 names them), not to a
complete pharmacopoeia.

**Phase 3 — the CLI.** `pull --drug-class`, `drug-class distribution`,
`drug-class diff-ancestors`, `stats --drug-class` / `--by drug-class`, cheat-sheet
queries.

**Phase 4 — the arm tier**, gated on Phase 0 question 4 and on someone measuring
the `outcome_groups` ↔ `design_groups` title join. Not before.

## What would make this the wrong thing to build

Stated so the decision to schedule it is a real decision:

* **If Phase 0 question 3 comes back low.** MeSH intervention coding is
  assigned by NLM after registration and is not universal. If a large share of
  Phase 3 studies carry no coded intervention, the axis is mostly
  `unclassified_agent` and the honest move is the curated `agent_names` layer
  over sponsor names — a hand-maintained drug dictionary, which is a
  substantially larger and more perishable commitment than this spec describes.
* **If the vocabulary starts chasing coverage.** The standing constraint that
  terms never come from coverage pressure is harder to hold here than anywhere
  else, because new drugs arrive continuously and each one is a visible gap.
* **If it becomes an ATC project.** Every request for finer classes points at
  ATC. The moment that is the answer, this stops being a MeSH-derivation spec
  and becomes an external-vocabulary integration with a licence question, and it
  should be re-argued from scratch rather than grown into.

## Standing constraints

* **Class is arm-level truth, rolled up to study level for convenience.** A
  study-tier row is never silently read as an arm-tier claim, and a control arm
  never inherits the study's experimental class.
* **Mechanism and structure are not the same axis.** `kind` is mandatory, and
  structural class is absent rather than approximated.
* **All matched classes are kept.** Precedence picks a primary; it does not
  discard the combination.
* **The ancestor diff reports, it does not reconcile.** Same rule as
  `ta diff-tree`.
* **`unclassified_agent` is a finding, not a bucket.** It goes to a review queue
  and is never pooled into a named class by any aggregate.
* **No new network dependency.** Everything above comes out of the payload the
  pull already fetches, or out of AACT tables alongside the ones it already
  reads. An external drug vocabulary is a different proposal.
