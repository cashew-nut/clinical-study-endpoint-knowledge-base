# Next session spec: close the step-2 gaps, then conform

Written at the end of build-order step 2 (the vocabularies). Two gaps block
step 3 (the conforming pipeline). This file is the spec for closing them.

Read this together with:

* the implementation plan (sections 2, 4, 5 and the build order in section 10)
* `vocab/README.md` — the vocabulary schema, the decisions behind it, and the
  "Known gaps" section this file expands on

---

## Gap 1 — the vocab sample is biased and can't be joined

`endpoints vocab sample` keeps the **500 most frequent distinct values per
field**, ordered `frequency DESC, value ASC`. Three consequences, all of which
bit during the step-2 vocabulary review:

1. **The singleton tail is alphabetically truncated.** In the 500-study sample
   the `measure` block held every value occurring twice or more (347 of them)
   plus only 153 of the frequency-1 values — and those 153 stop at "Ac…". The
   long tail of one-off endpoint wordings, which is where most rows actually
   live, is unrepresented and unrepresentable.
2. **Coverage is invisible.** The 500 kept `measure` values covered 1,175
   occurrences, while the `time_frame` block alone showed ≥4,014 outcome rows.
   So roughly three quarters of endpoint rows had no representation in the
   review artifact, and nothing in the CSV or the CLI output said so.
3. **The three field blocks can't be joined.** `measure`, `time_frame`, and
   `description` are exported as independent frequency tables. There is no way
   to ask "what measure had the time_frame 'Baseline through Week 52'?" — which
   is exactly the question needed to settle whether a baseline-anchored window
   is a change-from-baseline assessment or a safety collection period. That
   ambiguity is documented in `vocab/timepoint_patterns.yaml` and was resolved
   by linguistic convention rather than by evidence, because the evidence was
   not in the artifact.

### What to build

All in `src/clinical_endpoints/vocab/sample.py` + `cli/main.py` + tests.

**1a. Unbiased frequency export.** Replace the flat per-field `LIMIT` with:

| option | default | meaning |
|---|---|---|
| `--min-frequency N` | 2 | keep **every** distinct value occurring ≥ N times, uncapped |
| `--singleton-sample N` | 300 | keep a **random** sample of N values below `--min-frequency` |
| `--seed N` | 42 | seed for that sample, so re-running is reproducible |
| `--limit N` | 0 | hard cap per field; 0 = unlimited. Keep for backwards compatibility |

The random singleton sample is the point: an alphabetical head is biased in a
way that silently shapes the vocabulary, a seeded random sample is not.

**1b. Coverage reporting.** Both in the CLI output and as a `_coverage.csv`
sidecar (or a `coverage` block in a JSON summary — your call, but it must be
machine-readable, because the next vocabulary round should be able to diff it):

```
field         distinct_total  distinct_kept  rows_total  rows_covered  pct_rows_covered
measure                4812            647        4211          1583             37.6
```

**1c. Row-level export — the important one.** Add `--format rows` (default stays
`frequency`), writing one row per `raw.design_outcomes` record:

```
nct_id, outcome_type, measure, time_frame, description
```

sampled with the same `--seed`, capped by `--limit` (suggest 1000 rows). This is
what makes measure↔time_frame↔description co-occurrence reviewable, and it is
what the next vocabulary review round needs most.

**1d. `--outcome-type primary,secondary,other`** filter (default: all). Primary
outcomes are where efficacy endpoints concentrate; being able to review them
alone is useful.

### Done when

* `endpoints vocab sample --format rows --limit 1000` writes a joinable CSV
* `endpoints vocab sample` prints per-field coverage and writes it alongside
* the singleton sample is seeded and reproducible across runs
* tests cover: min-frequency boundary, seeded reproducibility, coverage
  arithmetic, and the row format

---

## Gap 2 — the therapeutic-area mapping has nothing to map

`vocab/ta_mesh_mapping.yaml` exists and validates, but **neither backend pulls
the conditions it maps.** `pull` writes only `raw.studies` and
`raw.design_outcomes`, so `pull --ta` hard-fails and no endpoint can be assigned
a therapeutic area. This blocks the `ta_id` column on `conformed.endpoints` and
the `IN_TA` graph edge.

### What to build

**2a. Pull the condition and intervention tables.** Both backends must produce
the *same* shapes, as they already do for studies/design_outcomes:

```
raw.browse_conditions      nct_id, mesh_term, mesh_term_normalised, mesh_type
raw.browse_interventions   nct_id, mesh_term, mesh_term_normalised, mesh_type
raw.conditions             nct_id, name          -- sponsor free text, not MeSH
```

* **AACT**: `aact.ctgov.browse_conditions`, `aact.ctgov.browse_interventions`,
  `aact.ctgov.conditions`, joined to `raw.studies` on `nct_id`, exactly like the
  existing `design_outcomes` pull.
* **CT.gov API**: already present in the study payload you are fetching — no
  extra requests. `derivedSection.conditionBrowseModule.meshes[]` (`id`, `term`)
  and `interventionBrowseModule.meshes[]`; sponsor conditions are at
  `protocolSection.conditionsModule.conditions[]`.

**2b. Tree numbers — the backend-specific part.**

* **AACT** publishes the MeSH thesaurus with tree numbers. Pull it into
  `raw.mesh_terms`. **Verify the schema before writing the query** — this was
  not verifiable when the mapping was written (AACT is egress-blocked from the
  build sandbox), so `ta_mesh_mapping.yaml`'s `tree_prefixes` table is written
  from the MeSH C/F branch structure and has never been checked against a live
  join. Run this first and adapt:

  ```sql
  SELECT column_name, data_type
  FROM information_schema.columns
  WHERE table_schema = 'ctgov'
    AND table_name IN ('browse_conditions', 'browse_interventions', 'mesh_terms')
  ORDER BY table_name, ordinal_position;

  SELECT * FROM ctgov.mesh_terms LIMIT 5;
  ```

  If `mesh_terms` has no `tree_number`, say so plainly and fall back to the
  descriptor and regex layers — do not invent a tree source.

* **CT.gov API** exposes only coarse branch letters, at
  `derivedSection.conditionBrowseModule.browseBranches[]` (`abbrev`, `name`),
  e.g. `BC04` = Neoplasms. Store them in
  `raw.browse_condition_branches (nct_id, branch_abbrev, branch_name)`. They are
  a one-character-deep tree prefix, so they can feed the same `tree_prefixes`
  layer at reduced precision.

**2c. Update `raw._pull_log`.** `SOURCE_TABLES` in `ingest/pull_log.py` is
hardcoded to `("studies", "design_outcomes")`. It must reflect what was actually
pulled, which now differs by backend — the API backend has no `mesh_terms`.

**2d. The TA resolver.** New module (suggest `src/clinical_endpoints/ta/`),
reading the `vocab.ta_mesh_*` tables that `vocab validate` already writes.
Apply the layers in the order `ta_mesh_mapping.yaml` documents:

1. `intervention_rules` against `raw.browse_interventions` (this is what makes a
   vaccine trial a vaccine trial — vaccine trials code their *condition* as the
   infection they prevent, never as "Vaccines")
2. `term_overrides` — exact descriptor match, `mesh_term_normalised`
3. `tree_prefixes` — longest prefix wins
4. `term_patterns` — regex on the descriptor, in file order
5. `defaults`

Write `conformed.study_therapeutic_area`:

```
nct_id, ta_id, rule_layer, matched_on, is_primary
```

Keep **all** matched areas (a lung-cancer trial is oncology *and* respiratory);
set `is_primary` by `therapeutic_areas.yaml` `precedence`, lowest wins, tie-break
by number of matching conditions per `resolution.tie_break`.

**2e. Enable `pull --ta`.** Both backends can filter at pull time: the API
returns conditions in the same payload, and AACT is a join. Remove the hard
failure in `cli/main.py`.

### Done when

* `endpoints pull --phase 3 --limit 500` lands conditions and interventions
* `endpoints pull --phase 3 --ta oncology` works and is logged in `_pull_log`
* `conformed.study_therapeutic_area` is populated with a plausible distribution
* re-running `pull` is still an idempotent refresh

---

## Task 3 — validate the tree prefixes against real data

This is the reason gap 2 matters beyond plumbing. Once conditions are pulled,
run every study's conditions through **the tree-prefix layer alone** and **the
regex layer alone**, and diff them:

```
mesh_term, tree_number, ta_from_tree, ta_from_pattern
```

Report every disagreement, most frequent first. Each one is either a wrong tree
prefix in `ta_mesh_mapping.yaml` or a wrong regex — both are worth fixing, and
this diff is the only way to find them without a MeSH expert. Do **not** silently
"fix" the YAML to make the diff empty; report it and propose specific edits.

Pay particular attention to the MeSH 2021 urogenital restructure noted in the
file: the old `C12` (male) / `C13` (female + pregnancy) split was merged into
`C12`. Both legacy and current prefixes are listed; confirm which the live
thesaurus uses.

---

## What to report back

1. Sample coverage before and after the fix — what fraction of endpoint rows the
   review artifact now represents.
2. The tree-vs-pattern disagreement list from task 3, with proposed YAML edits.
3. The therapeutic-area distribution across the pulled studies — if it looks
   nothing like a plausible Phase 3 mix, the mapping is wrong somewhere.
4. Whether `mesh_terms.tree_number` actually exists in AACT, and what the
   `browse_conditions` schema really is. Update `ta_mesh_mapping.yaml`'s
   `caveats` block either way.

## What NOT to do

* Do not start the conforming pipeline (build-order step 3). These two gaps plus
  a fresh vocabulary review come first; conforming against a vocabulary built
  from a biased sample would bake the bias in.
* Do not add or edit vocabulary terms to improve coverage numbers. New terms
  come from the human review round after the fresh sample, per plan section 5:
  "an unmatched or low-confidence endpoint … never silently becomes a new vocab
  term."
* Do not change the vocabulary schema. `vocab validate` and the reference-table
  fixtures in `tests/test_vocab_loader.py` should still pass untouched at the
  end of this session.
