# Design spec: corpus acquisition

> **Status: proposal, not implemented.** Nothing here is built. It proposes
> replacing the mechanism by which `pull` *obtains candidate studies* --
> `src/clinical_endpoints/ingest/ctgov_api.py`'s paginated scan and
> `src/clinical_endpoints/db.py`'s `attach_aact` -- while leaving the mapping
> layer (`ingest/aact.py`, `ingest/aact_results.py`, `ingest/design.py`,
> `ingest/results.py`, `ingest/interventions.py`) and everything downstream of
> `raw.*` untouched. Three unverified facts are flagged inline as **[needs one
> live check]**; each names the command that settles it.

Written after a pull was widened from `--limit 10000` to `--limit 100000` and
the FEV1 statistics did not move by a single arm. The diagnosis is not that
the limit was ignored (though it was). It is that **selection happens
downstream of a recency-ordered stream**, which makes `--limit` the wrong
control surface for the question the project exists to answer.

Read alongside:

* [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md)
  -- why biased sampling is treated as a defect here, not a tradeoff. That
  argument is load-bearing in §4 below.
* [`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md) and
  [`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) -- the two consumers
  that starve worst under the current mechanism.

---

## 1. The defect, stated exactly

`--ta` and `--drug-class` cannot be expressed by either source, so `pull`
fetches a stream and filters it client-side. Three properties of that stream
compound, and only the first is widely understood.

**1. The scan is capped well below `--limit`.** `ctgov_api.py:91` fixes
`MAX_PAGES = 25` against `PAGE_SIZE = 200`: 5,000 studies scanned, or 30,000
when `--ta`/`--drug-class` widens it to `MAX_PAGES_TA_FILTERED = 150`. The
early-exit target is `max(limit*3, limit+50)` (`:431`), unreachable for any
large limit, so the loop simply runs out of pages. `--limit 10000` and
`--limit 100000` issue *the same 25 requests*. Worse, the `hit_scan_cap`
warning the backend computes (`:529`) is only printed when `--ta` was given
(`cli/main.py:520-522`), so an unfiltered pull truncates in silence.

**2. The stream is ordered against the thing we want.** The query sorts
`StartDate:desc` (`:158`). The results tier needs studies that have *posted
results*, which happens roughly a year after primary completion -- so posted
results concentrate in the **old** end of the corpus while the scan is anchored
at the **new** end. Scanning deeper from a recency anchor buys the least
result-bearing studies available at every step. This is why removing `--org`
made things worse rather than better: the sponsor filter was server-side, so
the same page budget had been covering those sponsors' entire histories;
dropping it re-pointed the budget at the newest registrations worldwide.

**3. Every rejected study is paid for in full.** `_fetch_page` sends no
`fields` parameter, so each page returns complete study records -- results
section included (`ingest/results.py:404-412` says so outright). A
`--drug-class il5_inhibitor` scan therefore downloads up to 30,000 full
records, results sections and all, to keep perhaps a few dozen. The filter is
cheap; the bytes are not.

There is also a smaller leak worth fixing regardless: the client-side phase
re-check (`:447`) requires exact equality against the normalised phase list, so
`--phase 3` discards every `PHASE2/PHASE3` study the server-side `AREA[Phase]`
query legitimately returned, spending scan budget to throw away matches.

### Why no `--limit` fixes this

The registry holds over half a million studies. A 5,000-study scan is under
1% of it, drawn from the one end of the distribution where results do not
live. Raising the cap trades linearly against time and bytes while the yield
per scanned study stays near zero. **An ordering problem cannot be solved by
scanning further in the same order.**

---

## 2. What we are actually optimising

Two objectives, and they are not the same:

* **Recall per unit network** -- how many usable studies land per request.
* **Unbiasedness** -- whether what lands is a fair picture of what exists.

The current mechanism is poor at the first and silently bad at the second: the
corpus it produces is "whatever was registered most recently", which is a
sample nobody would choose, and its shape changes every time the cap is hit.
§4 shows that the most obvious fix for recall makes the bias dramatically
worse, which is why it is not the recommendation.

---

## 3. Option A -- make the scan cheap

Keep the architecture; stop overpaying for it.

* **`PAGE_SIZE` 200 -> 1000.** 1000 is the documented maximum, so this is a 5x
  cut in round trips for free. **[needs one live check]**: confirm with
  `curl 'https://clinicaltrials.gov/api/v2/studies?pageSize=1000&countTotal=true'`
  and check the returned page length.
* **Two-phase pull.** Phase 1 requests only the fields the filters read
  (`fields=NCTId,Phase,StartDate,LeadSponsorName,HasResults` plus the browse
  and arms modules) to build the surviving NCT id set; phase 2 fetches full
  records for survivors only. Given defect 3, this is where the order of
  magnitude is -- a rejected study should cost a few hundred bytes, not a full
  record with its results section.
* **Derive the page budget from `--limit`** instead of a flat constant, and
  report `hit_scan_cap` on *every* pull rather than only `--ta` ones.
* **Fix the phase re-check** to accept combined phases that contain a
  requested phase.

**Verdict.** Worth doing whatever else is decided -- it is a small, contained
change with no downside. But it is a constant-factor win against defects 1 and
3 and does nothing about defect 2. The corpus is still a recency window; it is
just a cheaper one.

---

## 4. Option B -- compile the vocabulary into the query

The claim that these axes "cannot be expressed server-side" is too strong. It
is true that CT.gov has no notion of `il5_inhibitor`. But this project already
holds the terms that *define* it, and those terms are exactly what the registry
indexes. From `vocab/drug_class_mesh_mapping.yaml:435-437` and
`vocab/drug_classes.yaml:598-604`:

```
il5_inhibitor  ->  mepolizumab, benralizumab, reslizumab
                   + "il-5 inhibitor", "anti-il-5", "il-5 receptor antagonist"
```

Six terms. So `--drug-class il5_inhibitor` could compile to an intervention
search over those six and return the matches directly, instead of scanning
30,000 studies hoping to trip over them. The same trick applies to `--ta` (MeSH
descriptors from `ta_mesh_mapping.yaml`) and would enable a `--measurement`
filter (`measurements.yaml:184-186` gives FEV1 ten synonyms and a regex).

Two rules if this is built:

1. **The server-side query is a recall device, never the authority.** The
   existing client-side resolver still decides what conforms, exactly as
   `run_pull` already re-checks every server-side filter. This preserves the
   invariant the code is careful about -- a pull-time match always agrees with
   the truth `pull` resolves afterward -- and it degrades safely: an
   unindexed synonym costs recall, not correctness.
2. **Only the high-precision layers are compilable.** `term_overrides`,
   `agent_names` and the class synonyms are specific enough to query. The INN
   stem patterns, MeSH ancestor rules and browse-branch rules
   (`drug_class_mesh_mapping.yaml` layers 3-5) are not -- they are how the
   resolver catches drugs nobody has curated yet.

**And that second rule is the reason this must not be the primary mechanism.**
A corpus assembled by querying your own vocabulary can only contain what your
vocabulary already knows. This project grows its vocabulary from what it
*fails* to match -- `measurements.yaml` alone declares
`on_unmatched: review_queue` precisely so unrecognised measurements surface
rather than being guessed at. Sampling only vocabulary-matching studies
starves that queue, and a vocabulary that can no longer discover its own gaps
is frozen. It reintroduces, at the acquisition layer, exactly the sampling
bias [`SAMPLING_AND_TA_RESOLUTION_SPEC.md`](SAMPLING_AND_TA_RESOLUTION_SPEC.md)
was written to eliminate at the review layer.

**Verdict.** A good *targeting* tool -- "pull me everything registered for
IL-5 this quarter" -- and it genuinely solves defect 2 for a named target. It
is the wrong thing to build the corpus out of.

---

## 5. Option C -- stop searching, take the corpus (recommended)

Both options above accept the premise that studies arrive through a search API
one page at a time. That premise is optional.

**AACT publishes a complete static copy of the database every night** -- a
`pg_dump` and a set of ~40 pipe-delimited flat files -- downloadable over plain
HTTPS, [with no account required](https://aact.ctti-clinicaltrials.org/downloads)
([flat-file instructions](https://aact.ctti-clinicaltrials.org/downloads/flatfiles_instructions)).
The 30 most recent daily snapshots stay available, plus permanent monthly
archives. That is the entire registry, protocol and results sections both, as
a file.

### The repo is already built for this

`ingest/aact.py` and `ingest/aact_results.py` read 21 tables from the AACT
`ctgov` schema:

```
studies  designs  eligibilities  sponsors  conditions  browse_conditions
browse_interventions  mesh_terms  design_groups  design_group_interventions
design_outcomes  interventions  intervention_other_names  outcomes
outcome_measurements  outcome_analyses  outcome_analysis_groups
outcome_counts  result_groups  baseline_measurements  ...
```

Every one of those is in the flat-file set, under the same names, with the same
columns. The mapping layer does not change. What changes is one function:
`db.py`'s `attach_aact` ATTACHes a remote Postgres and needs credentials plus
outbound 5432 -- which its own error message (`db.py:73-88`) admits is
"almost always a network reachability issue" behind a corporate firewall. A
sibling `attach_aact_snapshot(dir)` would instead create DuckDB views over the
downloaded files:

```sql
CREATE VIEW ctgov.studies AS
  SELECT * FROM read_csv('<dir>/studies.txt', delim='|', header=true,
                         sample_size=-1);
```

Same schema name, same table names, so `ingest/aact.py` runs unchanged.
**[needs one live check]**: confirm the flat-file naming, delimiter and quoting
by downloading one snapshot and running `DESCRIBE` over `studies.txt` and
`outcome_measurements.txt`.

### What this changes downstream

* **`--ta` / `--drug-class` stop being post-filters.** They become predicates
  over the complete corpus. There is no scan cap, no recency window and no
  starvation, because there is no stream to truncate.
* **`--limit` becomes a sampling convenience** rather than a ceiling on
  recall. "Increase the result set" stops being a network operation.
* **The ordering is ours to choose.** `ORDER BY start_date DESC` was never a
  requirement; it was a workaround for a stream that could not be indexed.
  Targeting the results tier becomes `WHERE has_results` -- the single
  highest-value predicate available, and one the current mechanism cannot
  express at all.
* **Pulls become reproducible.** A pull is "snapshot 2026-09-14 plus this
  predicate", re-runnable to the row. `raw._pull_log` already has the shape to
  record it: add the snapshot date to `filters_json`.
* **`vocab sample`'s unbiasedness guarantee becomes real**, because it can
  sample the corpus instead of a recency window.
* **The egress problem disappears**: one HTTPS GET instead of 150 paginated
  requests against a rate limit of roughly 50/minute. **[needs one live
  check]**: the published rate limit, which governs Option A's pacing too.

### Costs, honestly

* **Download size** -- a few GB per snapshot, and a refresh step to run. Not
  free, but bounded, and paid once per refresh rather than once per query.
* **Staleness** -- up to a day. Irrelevant for a historical variability
  library; relevant if someone wants this week's registrations, which is
  exactly the case Option B serves.
* **A new failure mode** -- a partial or superseded download. Mitigate by
  pinning and recording the snapshot date, and by validating row counts
  against the snapshot's own manifest before a pull reads it.
* **AACT lags CT.gov by a day and drops some API-only fields** -- notably the
  MeSH `ancestors[]`/`browseBranches[]` the CT.gov backend uses for drug-class
  layers 4-5 (`drug_class_mesh_mapping.yaml`'s header records this asymmetry).
  AACT gives full MeSH tree numbers instead, which `ingest/aact.py` already
  reads. The two backends already differ here; this does not make it worse.

---

## 6. Recommendation and phasing

**C as the backbone, A as an immediate and independent win, B only as a
targeting tool.** Gate each phase on a measurement, as
[`ENDPOINT_RESULTS_SPEC.md`](ENDPOINT_RESULTS_SPEC.md) and
[`DRUG_CLASS_SPEC.md`](DRUG_CLASS_SPEC.md) already do:

| phase | change | gate before the next phase |
|---|---|---|
| A1 | `PAGE_SIZE` 1000; `hit_scan_cap` reported on every pull; page budget from `--limit`; phase re-check accepts combined phases | a pull reports its true scan depth; `--limit` and studies landed agree or say why |
| C1 | `endpoints snapshot fetch` downloads and verifies an AACT snapshot; `attach_aact_snapshot` views it; `--source aact_snapshot` | `endpoints results coverage` run against the full corpus -- the four gate numbers, finally measured rather than owed |
| C2 | `--has-results`; `--limit` documented as sampling, not recall; snapshot date in `raw._pull_log.filters_json` | the FEV1 x IL-5 cell reports a number that is a property of the registry, not of scan depth |
| A2 | two-phase pull with `fields=` projection, for the live-API backend | bytes per landed study, before and after |
| B | vocabulary-compiled queries, live API only, as a targeting tool | recall against the C1 corpus as ground truth -- the only honest way to measure it |

C1 is the phase that answers the original complaint. Everything before it is
cheap, and everything after it is optional.

---

## 7. Standing constraints

Decisions a future change should not quietly undo:

1. **The client-side resolver stays the authority on what a study is.** Any
   server-side filter is a recall device. Never let a query decide
   conformance.
2. **Never build the corpus exclusively from vocabulary-derived queries.** A
   knowledge base that samples only what it already recognises cannot
   discover its own gaps (§4).
3. **`--limit` must never silently mean something smaller than it says.** If
   a mechanism cannot honour it, the pull says so -- on every pull, not only
   filtered ones.
4. **Recency ordering is an implementation detail, not a requirement.** It
   exists because a stream had to be truncated somewhere. Nothing downstream
   depends on it, and the results tier is actively harmed by it.
5. **Both backends keep landing the same `raw.*` shape.** The snapshot source
   is a third attachment, not a third schema.

---

## 8. What this does not solve

* **Results that were never posted.** No acquisition strategy invents them.
  The full corpus makes the denominator visible, which is the most that can be
  done.
* **Conformance recall.** If FEV1 endpoints are landing in
  `conformed.review_queue` rather than `conformed.endpoints`, a bigger corpus
  scales the problem rather than fixing it. Check
  `endpoints review list --reason measurement_unmatched` after C1 before
  concluding the corpus was the whole story.
* **Arm-level drug-class attribution.** `stats --drug-class` stays the study
  tier for the reasons `results/stats.py:123-134` gives. More studies does not
  change that.
