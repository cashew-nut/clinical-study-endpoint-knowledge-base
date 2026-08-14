# Roadmap and known gaps

## Blocking

**Connect live registry data.** Everything is built for it; nothing has run against it.
`clinicaltrials.gov` is blocked by this environment's egress policy. Once it is
allowlisted:

```bash
ceskb probe   --source ctgov --limit 50      # confirm the field paths first
ceskb refresh --source ctgov --incremental
```

`probe` exits non-zero if any declared field path fails to resolve. The paths in
`FIELD_PATHS` were written from API documentation rather than from observed responses, so
running `probe` before a bulk load is not optional. Expect the outcome-module paths to be
right and one or two of the peripheral ones (sponsor class, mesh terms) to need a nudge.

Two things should be reassessed against real data rather than assumed:

- **Coverage.** 98% on fixtures means nothing — the fixtures were written knowing the
  rules. Real registry text is far messier, and first-pass coverage on the full registry
  will more likely land somewhere in the 40–70% range, concentrated in the common
  endpoints. The Gaps view is built for exactly this.
- **Therapeutic area inference.** Substring matching against condition text is crude. It
  will over-assign on studies listing many conditions and miss ones phrased unusually.

---

## Near term

**Results data.** The results section of a registry record carries `unitOfMeasure` and
`paramType` (mean, median, geometric mean, …) per outcome measure. That is direct
evidence for the `unit` and `summary_measure` axes, which are currently the least
grounded parts of the model — they are inherited from concept defaults almost everywhere.
Ingesting results would let those axes be populated from what was actually reported
rather than from what the concept assumes. This is the single highest-value addition.

**Verify the external mappings.** Every UCUM code is marked `verified: false`, and the
measurement concepts have no LOINC or SNOMED mappings at all, because no terminology
service was reachable. This needs a pass with UCUM, LOINC and the NCI EVS API loaded. The
schema already enforces that a `verified: true` claim names what it was checked against.

**Widen the rule packs.** 52 rules across 46 concepts covers the common cases. The
long tail — pharmacokinetics, immunogenicity, device performance, health economics — is
untouched. Drive this from the Gaps view rather than by guessing.

**A review workflow.** Concepts carry `status: draft | reviewed | deprecated` but nothing
enforces it. Low-confidence classifications and new concepts should route to a named
reviewer, and the UI should let a reviewer accept or reject a specification with the
decision recorded alongside the rule evidence.

---

## Medium term

**Protocol and CSR sources.** Registry outcome text is a summary. Real protocols state
the estimand, the intercurrent event strategies, and the analysis population per
endpoint — the attributes currently marked `unspecified` almost everywhere. EU CTIS
publishes structured protocol data, and USDM documents themselves will increasingly be
available directly, which would let Layer C be *validated against* real sponsor output
rather than only generated.

**The industry context layer.** This was the stated longer-term goal and the model is
built for it. `measurement_modality` on every measurement concept is the attachment
point:

- **Procedures and equipment** — spirometer calibration standards, imaging acquisition
  protocols, central reading requirements. Hangs off `modality` and the
  `requires_calibrated_equipment` / `commonly_centrally_read` attributes already carried
  on those terms.
- **Thresholds in context** — minimal clinically important differences, regulatory
  precedent, guideline targets. The `definitional_threshold` structure is already
  separate from Layer B's per-study threshold, which is what makes "this study used 10%
  where the convention is 5%" a query rather than a manual comparison.
- **Instrument metadata** — PRO instrument versions, licensing, recall periods,
  validated translations. `requires_instrument: true` already flags where this is needed.

**Endpoint similarity.** Once several thousand studies are loaded, "which studies
measured the same thing in a comparable way" becomes answerable structurally — same
concept, same reference type, compatible timepoint — rather than by string matching. That
is the foundation for indirect comparison and for detecting silent incomparability, and
it is the thing this whole model exists to make possible.

---

## Deliberately not done

**A model-based classifier.** See `docs/DECISIONS.md` §3. A model belongs in the gap
queue proposing rules for review, not in the primary classification path, because the
audit trail is the product.

**Defaulting the unspecified attributes.** Assuming ITT, or randomisation as the anchor,
would raise apparent completeness and quietly corrupt every downstream comparison. See
§6.

**Modelling every endpoint that exists.** 46 concepts is a foundation, not an attempt at
coverage. The bias should stay towards fewer concepts with more parameters — see the
guidance in `docs/VOCABULARY.md`.
