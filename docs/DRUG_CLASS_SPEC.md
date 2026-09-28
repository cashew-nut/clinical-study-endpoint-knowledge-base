# Design spec: grouping endpoints by drug class

> **Status: implemented** (phases 1–4). The code is
> `ingest/interventions.py` and `ingest/aact_interventions.py` (landing),
> `vocab/drug_classes.yaml` and `vocab/drug_class_mesh_mapping.yaml` (the
> vocabulary), `drug_class/resolver.py` (resolution, the ancestor diff and the
> coverage summary), and the `pull --drug-class`, `endpoints drug-class` and
> `stats --drug-class` / `--by drug-class` surfaces in `cli/main.py` and
> `results/stats.py`.
>
> **Phase 0 was not done**: it is a measurement and it needs a live pull. This
> sandbox cannot reach clinicaltrials.gov or AACT (HTTP 403 on the outbound
> proxy), the same gate
> [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md#the-gate) and
> [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md#still-owed)
> are behind. Phases 1–4 were built anyway, each written to degrade rather than
> guess where a field it has never seen turns out to be absent.
> [Phasing, with a gate](#phasing-with-a-gate) records what the first live pull
> still owes. Read that section before trusting a coverage number from this
> axis.
>
> Grouping by drug class from ClinicalTrials.gov data is possible at three
> granularities, from fields already inside the payload `pull` fetches today,
> and is not possible at ATC granularity without an external vocabulary this
> project does not have. See
> [What the registry actually gives you](#what-the-registry-actually-gives-you).
>
> Four things changed during implementation and are amended in place below. A
> name-pattern layer (WHO INN stems) was added between the curated dictionary
> and the MeSH layers. `term_overrides` needed matching against the sponsor's
> intervention name as well as the NLM descriptor. The arm tier was built
> rather than deferred, because it degrades safely, though `stats` still does
> not consume it. And resolution, which runs inside `pull` and reads `vocab.*`,
> used to skip `conformed.study_drug_class` on a warehouse that had never been
> validated into, leaving another network pull as the only way to fill it in.
> `pull` now loads the vocabulary itself when the warehouse holds none. That
> load is additive only and never rewrites a vocabulary already present, so
> every run is still against a validated snapshot.

Read alongside:

* [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md) —
  the therapeutic-area axis, which this copies rather than reinvents
* [`../vocab/ta_mesh_mapping.yaml`](../vocab/ta_mesh_mapping.yaml) — the layered
  MeSH mapping this parallels, and its `caveats` block
* [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) — `endpoints stats`,
  where a drug-class axis is used and where a wrong class is most costly

---

## The short answer

| granularity | example | available? | from what |
|---|---|---|---|
| **modality** | drug / biologic / device / behavioural / procedure | **yes, exactly** | `armsInterventionsModule.interventions[].type`, AACT `ctgov.interventions.intervention_type` — a registry field, not an inference |
| **coarse pharmacologic class** | antineoplastic agents, cardiovascular agents, anti-infectives | **yes** | `derivedSection.interventionBrowseModule.browseBranches[]` — NLM-assigned, one hop from MeSH pharmacologic-action categories |
| **mechanism class** | PD-1 checkpoint inhibitor, GLP-1 receptor agonist, SGLT2 inhibitor | **yes, curated** | MeSH intervention descriptors, `ancestors[]`, and WHO INN stems, which turned out to matter most, mapped through a vocabulary this project writes as it does for therapeutic area |
| **ATC class** | `L01FF02`, `A10BJ` | **no** | ClinicalTrials.gov carries no ATC code, on either backend. Needs RxNorm or ATC: an external vocabulary, a network dependency, and a licensing question |
| **specific agent** | pembrolizumab | **yes**, as a string; **partly**, as a controlled id | MeSH descriptor when NLM assigned one; otherwise the sponsor's free-text `interventions[].name` plus `otherNames[]` |

The mechanism-class row is what this spec is about. It has the same shape as
therapeutic area: a study attribute nobody publishes, derivable from NLM's MeSH
coding through a layered mapping this project owns. It is built the same way.

## What the registry actually gives you

Four sources. Two were already landed and half-discarded, and two were not
landed at all.

The "landed before this spec" column records what `pull` landed before this
spec was implemented, which is what the cost estimate below was based on. All
four are landed now.

| source | CT.gov API v2 | AACT | landed before this spec |
|---|---|---|---|
| MeSH intervention descriptors | `derivedSection.interventionBrowseModule.meshes[]` | `ctgov.browse_interventions` | **yes** → `raw.browse_interventions` |
| MeSH ancestors of those descriptors | `interventionBrowseModule.ancestors[]` | not exposed as such | **no** — `_extract_mesh_rows` read only `meshes[]`. Now `raw.browse_intervention_ancestors` |
| coarse pharmacologic branches | `interventionBrowseModule.browseBranches[]` | no equivalent | **no** — `_extract_condition_branch_rows` was hardcoded to `conditionBrowseModule`. Now `raw.browse_intervention_branches` |
| the interventions themselves | `protocolSection.armsInterventionsModule.interventions[]` | `ctgov.interventions`, `ctgov.intervention_other_names`, `ctgov.design_group_interventions` | **no** — `pull` landed `armGroups[]` as `raw.design_groups` and dropped `interventions[]` on the floor. Now `raw.interventions`, `raw.intervention_other_names`, `raw.arm_interventions` |

Three consequences change the cost estimate:

**Most of this costs no network.** The CT.gov backend already fetches the whole
study record, since `_fetch_page` sends no `fields` parameter, so `ancestors[]`,
`browseBranches[]` and `interventions[]` are in a payload the pull has in hand
and throws away. The results tier found the same thing, as
[`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) records under "Landing
is on by default". The cost is warehouse size and extraction code, not request
volume.

**The AACT side needs three new tables, one of which carries the arm link.**
`ctgov.design_group_interventions` is a real join table between arms and
interventions, where the API backend can only approximate it by string-matching
`interventions[].armGroupLabels[]` against `armGroups[].label`. Following the
convention `_pull_mesh_terms` set, every column name here is introspected at
pull time rather than assumed, and a missing table costs the drug-class axis
rather than the pull.

**`raw.browse_interventions.mesh_type` is currently a constant.** Both backends
write the literal `'intervention'` into it (`ingest/aact.py`,
`ingest/ctgov_api.py`). If NLM's assignment list and its ancestor closure are
ever landed in the same table, that column has to start carrying the
distinction between `descriptor` and `ancestor`. Otherwise the two get pooled,
which converts "this trial studies pembrolizumab" into "this trial studies
antineoplastic agents" with no way to tell which was asserted. This spec lands
ancestors in a **separate table** instead, so the existing column's meaning
does not change under any query already written against it.

### What is not available at all

* **ATC codes.** Not in either backend, at any level. A trial of semaglutide
  carries `Semaglutide` (MeSH), not `A10BJ06`. Mapping to ATC means RxNorm or
  the WHO ATC index: an external vocabulary, an egress dependency two other
  specs already design around, and a licence question. Out of scope. If it is
  ever wanted, it belongs behind the curated vocabulary this spec proposes, as
  an attribute of a class term rather than as a second resolver.
* **Dose, route, schedule.** Free text in `interventions[].description` where
  stated at all. Not modellable at this cost.
* **Which arm "is" the experimental one**, as an assertion. `armGroups[].type`
  (`EXPERIMENTAL`, `ACTIVE_COMPARATOR`, `PLACEBO_COMPARATOR`, `SHAM_COMPARATOR`,
  `NO_INTERVENTION`, `OTHER`) is landed today as `raw.design_groups.group_type`
  and is the closest thing. It is a registry field and mostly reliable, but
  `OTHER` is common and multi-arm platform trials abuse it. Treat it as
  evidence rather than as truth; see
  [Class is an arm property](#class-is-an-arm-property-not-a-study-property).

## The modelling decision: class means mechanism, not chemistry

Everything else depends on this decision. Getting it wrong produces groupings
that are arithmetically fine and clinically useless.

MeSH classifies a substance twice: structurally, by what it is made of, so that
metformin is a biguanide in the D tree, and functionally, by what it does, so
that metformin is a hypoglycemic agent by pharmacologic action. Walking the
MeSH ancestors and calling them all classes mixes both into one column, and
then `GROUP BY drug_class` puts a sulfonylurea and a biguanide in different
groups while putting two unrelated biguanides in the same one.

For grouping endpoints, mechanism is the axis that carries signal: two drugs
that lower HbA1c through the same receptor should behave alike on an HbA1c
endpoint, and two drugs that share a benzene ring should not. So:

* `drug_classes.yaml` terms carry a mandatory `kind`, one of
  **`mechanism`** (GLP-1 receptor agonist, PD-1 inhibitor, SGLT2 inhibitor),
  **`pharmacologic`** (antineoplastic agent, antihypertensive — the coarse
  action level CT.gov's browse branches give directly),
  **`modality`** (small molecule, monoclonal antibody, cell therapy, vaccine,
  device, behavioural), or
  **`control`** (placebo, standard of care, no intervention).
* `kind` is mandatory so that every query says which axis it means, the same
  way `COMPOSITE_ENDPOINTS_SPEC.md` makes composite `kind` mandatory so a
  comparability query cannot pool an event union with a scored index.
* **Structural class is not modelled**, and is absent rather than approximated.
  If a use for it appears, it is a fourth `kind`, not a redefinition of the
  other three.

`control` is a first-class kind because half the arms in a placebo-controlled
corpus are placebo arms. An axis that cannot name them cannot exclude them, and
what a class does relative to placebo is the first question asked of this
axis.

## Class is an arm property, not a study property

A study does not have a drug class. Its **arms** do, and they differ from each
other by construction, which is what a controlled trial is.

Two tiers, with the tier recorded on every row:

| tier | what it asserts | needs |
|---|---|---|
| **study** | this study's interventions include class X | `raw.browse_interventions` + `raw.interventions` — always available |
| **arm** | *this arm* received class X | the arm↔intervention link: AACT's `design_group_interventions`, or the API's `armGroupLabels[]` matched to `armGroups[].label` |

The study tier is cheap, always available, and answers which trials involve a
GLP-1 agonist. The arm tier is what `endpoints stats` needs, because a
dispersion row is per arm: attaching the study's class set to a placebo arm's
SD produces a wrong number with a confident denominator.

**The arm tier degrades, it does not guess.** On the API backend the link is a
string match between `interventions[].armGroupLabels[]` and `armGroups[].label`;
where a label does not match exactly (after the same normalisation the
conformance engine already uses), the arm gets no class rather than the study's.
`link_method` on `conformed.arm_drug_class` records which path produced the row —
`join_table` (AACT), `arm_label` (API, exact after normalisation), or nothing at
all — so a query can demand the strong one.

A second fragility sits downstream: the results section's
`outcome_groups.group_key` is the results group id, not a protocol arm id, and
the two are linked only by title. That join is out of this spec's scope. Until
someone measures how often the titles agree, `stats --drug-class` is specified
against the **study** tier, with the arm tier feeding
`conformed.arm_drug_class` and waiting for that measurement.

## Schema

Every piece is optional and additive: nothing here changes an existing table's
meaning, and every table can be absent without breaking a command that does not
ask for it.

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
loader uses, rather than trusted from a source column, as `ingest/aact.py`
does when it declines to trust an unverified `downcase_mesh_term`.

`raw.browse_intervention_branches` mirrors `raw.browse_condition_branches`
exactly, including its unverified-abbreviation caveat. The condition branches
turned out to be `B` + a MeSH tree code (`BC04` = Neoplasms); the **intervention
branch abbreviations are not that shape** and are not documented anywhere this
project can reach. Phase 0 has to observe them rather than guess; see the
gate.

### New vocabulary files

Two, matching the split therapeutic area already uses (a term list, and a
mapping that is not a term list):

* **`vocab/drug_classes.yaml`** — a new `DimensionSpec` in `vocab/schema.py`:
  columns `id, label, inline_label, definition, kind, parent, precedence,
  notes`, with `parent` a self-reference validated for cycles. One shallow
  level of nesting (`pd1_inhibitor` → `checkpoint_inhibitor` →
  `antineoplastic_immunological`) is enough. This is not the recursive
  structure `COMPOSITE_ENDPOINTS_SPEC.md` argues for and should not grow into
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
`conformed.study_therapeutic_area` plus `kind`, so a query written against one
works against the other with the table name changed.

## The resolver

`src/clinical_endpoints/drug_class/resolver.py`, structured as
`ta/resolver.py` is, including the split that lets pull-time filtering and bulk
resolution share one function (`resolve_study_drug_class_matches`) so a
`--drug-class` pull can never disagree with the table written afterwards.

Layers, first hit wins **per intervention**, every layer's matches kept:

| # | layer | against | why it is where it is |
|---|---|---|---|
| 0 | `control_rules` | `raw.interventions.name` | placebo, sham and standard of care first, so a placebo arm is never classed by a MeSH term the sponsor attached to the study as a whole. A control match **short-circuits** its intervention: a placebo is not a small molecule with a placebo mechanism |
| 1 | `term_overrides` | drug name, exact, against the NLM descriptor **and** the sponsor's `name` or `other_name` | where multi-class ambiguity is settled by hand |
| 2 | `agent_names` | the same two sources | the curated dictionary: the sponsor's own drug name and its aliases, for agents NLM has not coded (new molecular entities are routinely uncoded for a year or more) |
| 3 | `name_patterns` | the same two sources | **WHO INN stems**, plus the vaccine-platform words no stem covers. Added during implementation; see below |
| 4 | `ancestor_rules` | `raw.browse_intervention_ancestors` | NLM's own hierarchy, the mechanism signal where it exists |
| 5 | `branch_rules` | `raw.browse_intervention_branches` | the coarse pharmacologic level, always `kind: pharmacologic` and never a mechanism claim |
| 6 | `modality_rules` | `intervention_type` | the registry field, as a fallback **within the modality axis only**; see below |
| 7 | `defaults` | — | `unclassified_agent` for an intervention with no match, or `no_interventions_stated` |

The curated layers sit **above** the MeSH ones, which is the one ordering
choice not copied from the TA resolver. A curated agent-to-class entry is a
human assertion about a specific drug, while an ancestor match is an inference
from NLM's coding of it, so where they disagree the human wins. The
disagreement is surfaced by `drug-class diff-ancestors` below.

**Three amendments the implementation forced.**

*`name_patterns` (layer 3) was not in the original design.* WHO International
Nonproprietary Name stems are a published, enforced naming convention, so
`-gliflozin` is not a guess about a drug: it is the stem that made the drug's
name what it is. That makes it the layer most likely to class an agent NLM has
not coded yet, which is the gap the curated dictionary cannot close, and the
reason a 2027 SGLT2 inhibitor still lands without a vocabulary edit. It sits
below `agent_names` because a specific assertion beats a stem, and its loosest
patterns are confined to the modality axis, where being wrong is cheapest.

*`term_overrides` matches the sponsor's name too, not just the NLM descriptor.*
The original design keyed it on descriptors alone. For interventions the
descriptor is a drug name and sponsors usually write the same string, so an
override keyed only on the NLM side would settle half the corpus at random: a
study registering "Aspirin" would miss the antiplatelet override that a study
NLM coded as "Aspirin" gets.

*Matching is first-hit-wins per source item **and per `kind`**.* The spec said
per intervention, which was too strong: it would make a mechanism match
suppress the modality match that should sit alongside it. Within one source
item, the first layer to match a given `kind` wins that kind, and later layers
can still fill the kinds still open. So pembrolizumab is `pd1_inhibitor` as a
mechanism and `monoclonal_antibody` as a modality. `modality_rules` is the one
layer that is a fallback within its own axis rather than a competitor, which is
what stops a `-mab` also being called a small molecule.

**The source items are not all the same grain.** An intervention is a row of
its own, while a MeSH descriptor, an ancestor and a browse branch are attached
to the study with no link back to which intervention they describe. So the
intervention layers run per intervention, the MeSH layers run per study, and
their matches are unioned. A study-level match is a weaker claim, `rule_layer`
records which it was, and the arm tier uses only the intervention-level half.

**Keep all matches; precedence picks a primary.** Combination therapy is
standard of care in oncology and increasingly in diabetes, so a trial of
pembrolizumab plus chemotherapy is both a checkpoint-inhibitor trial and a
cytotoxic-chemotherapy trial. Collapsing to one would be the mistake
`therapeutic_areas.yaml` refuses for a lung-cancer trial.

**Unclassified is visible, never silent.** An intervention that reaches layer 7
lands in `conformed.drug_class_review_queue` as well as taking the default, so
the coverage number and the work queue are the same list, as
`conformed.review_queue` already is on the endpoint side.

## CLI surface

```bash
endpoints pull --phase 3 --drug-class glp1_receptor_agonist   # client-side, before --limit
endpoints drug-class distribution                             # what the corpus is made of
endpoints drug-class distribution --kind mechanism            # ...on one axis only
endpoints drug-class coverage                                 # the denominator, and what it missed
endpoints drug-class diff-ancestors --out drug_class_diff.csv # curated vs NLM disagreements
endpoints stats --measurement hba1c --drug-class glp1_receptor_agonist
endpoints stats --measurement hba1c --by drug-class           # stratify instead of filter
endpoints stats --measurement hba1c --by drug-class --analyses
```

`drug-class coverage` was not in the original design. It plays the same role
`results coverage` does: a distribution without a denominator makes a thin axis
look complete, so the denominator line is printed by `distribution` too and
cannot be turned off. `coverage` additionally lists the most frequent
unclassified interventions, which are the input to the next vocabulary round,
the role `endpoints review list` plays on the endpoint side.

`--drug-class` on `pull` is a client-side filter applied before `--limit`, for
the reason `--ta` is (`MAX_PAGES_TA_FILTERED`): neither backend can express it
server-side, and filtering after truncation starves a narrow class of matches
it actually has. It reuses `--ta`'s scan-cap machinery rather than growing its
own.

`diff-ancestors` is the counterpart of `endpoints ta diff-tree` and has the
same discipline: it **reports disagreements between the curated layer and NLM's
ancestry and does not reconcile them**. Editing the YAML until the diff is
empty destroys the only external check this axis has.

## Where a drug-class axis changes what a number means

**On dispersion, the default `stats` output, drug class is a diagnostic rather
than a grouping key.** The SD of change-from-baseline HbA1c is mostly a
property of the population, the assay and the timepoint rather than of the
drug, so splitting an already thin SD library by drug class mostly buys smaller
denominators. Its use runs the other way: an SD that differs sharply by class
is evidence the groups are not exchangeable and the pooled number was already
wrong. Hence `--by drug-class` as a stratifier alongside `--drug-class` as a
filter.

**On `--analyses`, effect sizes, drug class is load-bearing.** Pooling
treatment effects across mechanisms is a category error rather than a variance
question: the median effect on HbA1c across all drugs has no referent. Any
effect-size distribution this project reports is class-aware or says it is
not.

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
   `measurement_id` × `drug_class_id` within one therapeutic area. Endpoint
   choice is a class-level convention, and this is how to see it.
3. **"Which classes have adopted PFS over OS, and when?"** — the same cross-tab
   with `start_date`.
4. **"Placebo-arm variability for this endpoint"** — `kind: control` plus the
   arm tier, a quantity a sample-size calculation needs and usually
   reconstructs by hand.
5. **"Is my endpoint conventional for this class?"** — the frequency of a
   measurement within a class, versus outside it.

## Phasing, with a gate

**Phase 0 — measure, before writing any vocabulary.** One live pull of ~500
Phase 3 studies, then four counts:

| # | question | why it gates the rest |
|---|---|---|
| 1 | Does `interventionBrowseModule` carry `ancestors[]`, and what is in it? | Layer 4 is NLM's own mechanism signal. If ancestors are absent or purely structural, mechanism classes rest entirely on the curated and stem layers, and the vocabulary is a much bigger hand-maintained object |
| 2 | What do the intervention `browseBranches[]` names look like? | `BC04`-style abbreviation parsing is condition-specific, so layer 5 matches on `branch_name` instead — but the names themselves are still unobserved |
| 3 | What share of studies have ≥1 MeSH-coded intervention at all? | The ceiling on every MeSH-derived layer. `raw.browse_interventions` is already landed, so this one is answerable from an existing pull |
| 4 | Does the arm↔intervention link resolve, and how often? | Decides whether the arm tier is built now or deferred. AACT: does `design_group_interventions` exist? API: what share of `armGroupLabels[]` match an `armGroups[].label` exactly after normalisation? |

**None of the four has been run.** Question 3 is answerable the moment any
warehouse with a real pull exists. Questions 1, 2 and 4 need a pull from an
environment with egress, the same blocker three other specs record. The code
was written to survive each answer rather than to assume one:

| # | if the answer is bad | what happens |
|---|---|---|
| 1 | `ancestors[]` absent | `raw.browse_intervention_ancestors` stays empty, layer 4 contributes nothing, and mechanism classes rest on the curated and stem layers (which is already the situation on every AACT pull). `drug-class diff-ancestors` says so rather than reporting an empty diff as agreement |
| 2 | the branch abbreviations are a shape nobody expected | nothing breaks: `branch_rules` matches on `branch_name`, never on a parsed `branch_abbrev`. That is the one lesson `BC04` taught, applied before it could bite twice |
| 3 | MeSH intervention coding is sparse | the axis is mostly `unclassified_agent`, `drug-class coverage` reports that, and the review queue names the agents to add. This is the kill criterion; see [What would make this the wrong thing to build](#what-would-make-this-the-wrong-thing-to-build) |
| 4 | the arm link rarely resolves | `conformed.arm_drug_class` is small and `link_method` says why; nothing downstream depends on it yet |

A vocabulary written against guessed field shapes is the failure mode
`ta_mesh_mapping.yaml`'s caveats block exists to prevent, so
`drug_class_mesh_mapping.yaml` carries its own caveats block saying, first line,
that not one rule in it has been run against a live pull.

**Phase 1 — land it. Done.** The five raw tables, both backends, introspected
rather than assumed, extraction-only. `raw.interventions` alone makes "which
trials studied drug X" a query, with no vocabulary at all. On the CT.gov side
this costs no network: `_fetch_page` sends no `fields` parameter, so the whole
`armsInterventionsModule` and `derivedSection` were already in the payload and
being discarded.

**Phase 2 — the vocabulary and the study tier. Done.** `drug_classes.yaml` (116
terms: 86 mechanism, 16 pharmacologic, 10 modality, 4 control),
`drug_class_mesh_mapping.yaml` (407 curated agents, 61 ancestor rules, 91
patterns), `drug_class/resolver.py`, `conformed.study_drug_class`,
`conformed.drug_class_review_queue`. The vocabulary is scoped to the classes a
Phase 3 corpus is mostly made of across the areas `therapeutic_areas.yaml`
already covers, rather than to what the corpus contains, because Phase 0
question 3 could not be run. That is the largest thing the first live pull will
revise.

**Phase 3 — the CLI. Done.** `pull --drug-class`, `drug-class distribution`,
`drug-class coverage`, `drug-class diff-ancestors`, `stats --drug-class` /
`--by drug-class`, cheat-sheet queries.

**Phase 4 — the arm tier. Built, and half-gated.**
`conformed.arm_drug_class` is written, because the resolution is deterministic
and degrades safely: the strong link (AACT's `design_group_interventions`) and
the weak one (CT.gov's `armGroupLabels` matched to an `armGroups.label`) are
distinguished by `link_method`, and a label that matches no arm produces no row
rather than a wrong one. What stays gated is the half that needed the
measurement: **`endpoints stats` does not consume the arm tier**, and
`--drug-class` there is the study tier, because the `outcome_groups` to
`design_groups` title join is still unmeasured. The gate is recorded in
`write_arm_drug_class`'s docstring.

That title join now exists as `conformed.result_group_arm` (written by
`results conform`, see `results/arms.py`), and `results coverage` section 5
measures it. `stats --arm-role` / `--by arm-role` consume it, but only for the
arm's **role** (experimental or control, from `armGroups[].type`), and each
arm-selected report prints its own link rate. The arm tier's drug class uses
this table in one place only: as fallback evidence that an arm typed `OTHER`
is a control. Routing `--drug-class` through the arm tier stays gated until
section 5 has been read on a live pull.

## What would make this the wrong thing to build

* **If Phase 0 question 3 comes back low.** MeSH intervention coding is
  assigned by NLM after registration and is not universal. If a large share of
  Phase 3 studies carry no coded intervention, the axis is mostly
  `unclassified_agent` and the remaining option is the curated `agent_names`
  layer over sponsor names, a hand-maintained drug dictionary. That is a
  substantially larger and more perishable commitment than this spec
  describes.
* **If the vocabulary starts chasing coverage.** The standing constraint that
  terms never come from coverage pressure is harder to hold here than
  elsewhere, because new drugs arrive continuously and each one is a visible
  gap.
* **If it becomes an ATC project.** Every request for finer classes points at
  ATC. Once that is the answer, this stops being a MeSH-derivation spec and
  becomes an external-vocabulary integration with a licence question, to be
  re-argued from scratch rather than grown into.

## Standing constraints

* **Class is arm-level truth, rolled up to study level for convenience.** A
  study-tier row is never read as an arm-tier claim, and a control arm never
  inherits the study's experimental class.
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
* **A control match short-circuits its intervention.** No later layer may add to
  a placebo, sham, standard-of-care or no-intervention row.
* **A child class outranks its parent.** `pd1_inhibitor` must beat
  `checkpoint_inhibitor` for the primary, or the specific claim the mechanism
  axis exists for is thrown away. `vocab validate` enforces this, along with the
  rule that a parent never changes `kind`.
* **`stats` uses the study tier.** Until the `outcome_groups` ↔ `design_groups`
  title join is measured, an arm-level SD is a number built on an unmeasured
  join. Changing this needs the measurement first, not a plausible-looking join.
* **`raw.browse_interventions.mesh_type` keeps its meaning.** Ancestors live in
  their own table. Pooling NLM's assignment list with its ancestor closure would
  silently convert "this trial studies pembrolizumab" into "this trial studies
  antineoplastic agents".
