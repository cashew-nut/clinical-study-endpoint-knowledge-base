# Design spec: unbiased vocabulary sampling and therapeutic-area resolution

> **Status: implemented.** Lives in
> `src/clinical_endpoints/vocab/sample.py`, `src/clinical_endpoints/ta/`, both
> ingestion backends, and `endpoints vocab sample` / `pull --ta` /
> `ta diff-tree`. One measurement is still owed -- see
> [Still owed](#still-owed).

Written after the first vocabulary round, when two defects blocked conforming:
the review artifact the vocabulary was built from was biased and unjoinable,
and the therapeutic-area mapping had nothing to map. Both are fixed; this file
records what the fix is and why it is shaped that way, because both decisions
are easy to undo by accident.

Read alongside:

* [`../vocab/README.md`](../vocab/README.md) -- the vocabulary schema and its
  known gaps
* [`USAGE.md`](USAGE.md) -- how to run `vocab sample`, `pull --ta` and
  `ta diff-tree`

---

## 1. Sampling: an alphabetical head is a biased vocabulary

`endpoints vocab sample` originally kept the **500 most frequent distinct
values per field**, ordered `frequency DESC, value ASC`. Three consequences,
all of which bit during the first vocabulary round:

1. **The singleton tail was alphabetically truncated.** In a 500-study sample
   the `measure` block held every value occurring twice or more (347 of them)
   plus only 153 of the frequency-1 values -- and those 153 stopped at "Ac…".
   The long tail of one-off endpoint wordings, which is where most rows
   actually live, was unrepresented and unrepresentable.
2. **Coverage was invisible.** The 500 kept `measure` values covered 1,175
   occurrences, while the `time_frame` block alone showed ≥4,014 outcome rows.
   Roughly three quarters of endpoint rows had no representation in the review
   artifact, and nothing in the CSV or the CLI output said so.
3. **The three field blocks could not be joined.** `measure`, `time_frame` and
   `description` were independent frequency tables, so "what measure had the
   time_frame 'Baseline through Week 52'?" was unanswerable -- which is exactly
   the question that settles whether a baseline-anchored window is a
   change-from-baseline assessment or a safety collection period. That
   ambiguity was resolved by linguistic convention rather than by evidence,
   because the evidence was not in the artifact.

### What was built

**Unbiased frequency export.** The flat per-field `LIMIT` is gone:

| option | default | meaning |
|---|---|---|
| `--min-frequency N` | 2 | keep **every** distinct value occurring ≥ N times, uncapped |
| `--singleton-sample N` | 300 | keep a **random** sample of N values below `--min-frequency` |
| `--seed N` | 42 | seed for that sample, so re-running is reproducible |
| `--limit N` | 0 | hard cap per field; 0 = unlimited, kept for backwards compatibility |

The seeded random singleton sample is the point: an alphabetical head is biased
in a way that silently shapes the vocabulary, a seeded random sample is not.

**Coverage reporting**, in the CLI output and as a machine-readable
`<out>_coverage.csv` sidecar, so the next vocabulary round can diff it against
this one:

```
field         distinct_total  distinct_kept  rows_total  rows_covered  pct_rows_covered
measure                4812            647        4211          1583             37.6
```

**Row-level export** -- `--format rows`, one row per `raw.design_outcomes`
record (`nct_id, outcome_type, measure, time_frame, description`), sampled with
the same `--seed` and capped by `--limit`. This is what makes
measure↔time_frame↔description co-occurrence reviewable, and it is what the
second vocabulary round leaned on most.

**`--outcome-type primary,secondary,other`** (default: all), because primary
outcomes are where efficacy endpoints concentrate.

Round two was rebuilt against this sampler, over 13,542 outcome rows; the
resulting coverage figures are in [`../vocab/README.md`](../vocab/README.md).

## 2. Therapeutic areas: the mapping had nothing to map

`vocab/ta_mesh_mapping.yaml` existed and validated, but neither backend pulled
the conditions it maps. `pull` wrote only `raw.studies` and
`raw.design_outcomes`, so `pull --ta` hard-failed and no endpoint could be
assigned a therapeutic area.

### What was built

**Conditions and interventions, same shape from both backends:**

```
raw.browse_conditions      nct_id, mesh_term, mesh_term_normalised, mesh_type
raw.browse_interventions   nct_id, mesh_term, mesh_term_normalised, mesh_type
raw.conditions             nct_id, name          -- sponsor free text, not MeSH
```

AACT joins `ctgov.browse_conditions` / `browse_interventions` / `conditions` to
`raw.studies` on `nct_id`, exactly like `design_outcomes`. The CT.gov API
backend reads them out of the study payload it already fetches
(`derivedSection.conditionBrowseModule.meshes[]`,
`interventionBrowseModule.meshes[]`,
`protocolSection.conditionsModule.conditions[]`) -- no extra requests.

**Tree numbers, which are backend-specific.** AACT publishes the MeSH thesaurus
with tree numbers, pulled into `raw.mesh_terms`. The CT.gov API exposes only
coarse branch letters (`derivedSection.conditionBrowseModule.browseBranches[]`,
e.g. `BC04` = Neoplasms), landed in
`raw.browse_condition_branches (nct_id, branch_abbrev, branch_name)`; they are
a one-character-deep tree prefix, so they feed the same `tree_prefixes` layer
at reduced precision.

**`raw._pull_log.source_tables`** reflects what was actually pulled, which now
differs by backend -- the API backend has no `mesh_terms`.

**The resolver** (`src/clinical_endpoints/ta/resolver.py`) reads the
`vocab.ta_mesh_*` tables that `vocab validate` writes, and applies the layers in
the order `ta_mesh_mapping.yaml` documents, first hit winning *per condition or
intervention*:

1. `intervention_rules` against `raw.browse_interventions` -- this is what makes
   a vaccine trial a vaccine trial, since vaccine trials code their *condition*
   as the infection they prevent, never as "Vaccines"
2. `term_overrides` -- exact descriptor match on `mesh_term_normalised`
3. `tree_prefixes` -- longest prefix wins
4. `term_patterns` -- regex on the descriptor, in file order
5. `defaults`

It writes `conformed.study_therapeutic_area (nct_id, ta_id, rule_layer,
matched_on, is_primary)`, keeping **all** matched areas -- a lung-cancer trial
is oncology *and* respiratory -- with `is_primary` set by
`therapeutic_areas.yaml` precedence, lowest wins, tie-broken by number of
matching conditions per `resolution.tie_break`.

**`pull --ta`** filters at pull time on both backends -- *before* `--limit`
truncates, not after, since truncating first would starve a smaller area of
matches it actually has (registrations skew toward whichever conditions
dominate trial activity generally). Both backends resolve each candidate
study with `resolve_study_ta_matches` (the same per-study match `ta/resolver.py`
uses to write `conformed.study_therapeutic_area`) while scanning, up to a cap
(`MAX_PAGES_TA_FILTERED` / `TA_MAX_SCANNED`) -- see `docs/USAGE.md`'s
"Therapeutic areas" section. `pull` resolves therapeutic areas for every
pulled study whether or not `--ta` is given.

## 3. Validating the tree prefixes against real data

The reason §2 matters beyond plumbing. `endpoints ta diff-tree` runs **the
tree-prefix layer alone** and **the regex layer alone** over every pulled
study's conditions and reports every disagreement, most frequent first:

```
mesh_term, tree_number, ta_from_tree, ta_from_pattern
```

Each disagreement is either a wrong tree prefix in `ta_mesh_mapping.yaml` or a
wrong regex -- both worth fixing, and this diff is the only way to find them
without a MeSH expert. The tool reports; it does not "fix" the YAML to make the
diff empty.

Worth particular attention when the diff is first run for real: the MeSH 2021
urogenital restructure noted in the file, where the old `C12` (male) / `C13`
(female + pregnancy) split was merged into `C12`. Both legacy and current
prefixes are listed, and which one the live thesaurus uses is still unconfirmed.

## Still owed

All of the above runs; two things can only be answered against a live pull, and
**neither ingestion backend has been reachable from the build sandbox** (AACT
port 5432 closed, `clinicaltrials.gov` unreachable) -- the same constraint
`vocab/README.md`'s "Known gaps" records:

1. **The tree-vs-pattern disagreement list**, and the specific
   `ta_mesh_mapping.yaml` edits it implies.
2. **Whether `mesh_terms.tree_number` actually exists in AACT**, and what the
   `browse_conditions` schema really is. `ta_mesh_mapping.yaml`'s `caveats`
   block should be updated either way. If `mesh_terms` has no tree number, the
   honest fallback is the descriptor and regex layers -- not an invented tree
   source.

A third check is cheap and worth doing on the first real pull: the
therapeutic-area distribution across pulled studies. If it looks nothing like a
plausible Phase 3 mix, the mapping is wrong somewhere.

## Standing constraints

These held while this spec was being built and still hold:

* **Vocabulary terms do not come from coverage pressure.** An unmatched or
  low-confidence endpoint goes to the review queue; it never silently becomes a
  new vocab term. New terms come from a human review round against a fresh
  sample.
* **The sampler is not a place to fix the vocabulary.** If coverage is bad, the
  answer is a review round, not a narrower sample.
* **The tree diff reports, it does not reconcile.** Editing the YAML to empty
  the diff destroys the only signal there is.
