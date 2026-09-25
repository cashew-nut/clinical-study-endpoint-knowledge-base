"""`endpoints help [TOPIC]`: a usage cheat sheet printed from the CLI.

Each topic holds worked examples. `endpoints <command> --help` lists every
option; docs/USAGE.md has the full explanation.
"""

from __future__ import annotations

OVERVIEW = """\
[bold]endpoints[/bold] -- clinical-study endpoint library

[bold]Typical workflow[/bold]
  uv run endpoints vocab validate                    [dim]# load vocab/*.yaml into the warehouse[/dim]
  uv run endpoints pull --phase 3 --limit 500        [dim]# land studies into raw.*[/dim]
  uv run endpoints conform                           [dim]# planned endpoints -> conformed.endpoints[/dim]
  uv run endpoints results conform                   [dim]# results section -> SD estimates[/dim]
  uv run endpoints stats --measurement fev1          [dim]# the variability distribution[/dim]

[bold]Commands[/bold]
  pull                  land studies from ClinicalTrials.gov (or AACT)
  conform               conform planned endpoints against the vocabulary
  stats                 SD / effect-size distributions for an endpoint
  results conform       conform the results section
  results coverage      how much of the results section is usable
  drug-class ...        distribution | coverage | diff-ancestors
  ta diff-tree          tree-prefix vs regex therapeutic-area disagreements
  usdm show | coverage  the USDM 4.0 projection
  review list           the conform review queue
  vocab validate | sample
  serve                 the read-only USDM API

[bold]Study filters[/bold] (every reporting command; see `endpoints help filters`)
  --ta  --org  --phase  --drug-class  --since

[bold]More[/bold]
  uv run endpoints help <topic>     topics: {topics}
  uv run endpoints <command> --help every option of one command
  docs/USAGE.md                     the long form
"""

FILTERS = """\
[bold]Study filters[/bold] -- the same flags, the same meaning, on every command that reads
the warehouse: stats, results coverage, drug-class distribution | coverage |
diff-ancestors, ta diff-tree, usdm coverage, review list, vocab sample.
`pull` takes them too, to decide what to land.

  --ta respiratory,oncology     any therapeutic area the study resolved to
  --org "Pfizer,AbbVie"         lead-sponsor name fragment, case-insensitive
  --phase 3 | 2/3 | 1/2,2,3     exact phase (3 does not include 2/3); PHASE3 also works
  --drug-class sglt2_inhibitor  any drug class the study resolved to (study tier)
  --since 2020-01-01            start_date on or after

Values within one flag are OR'd; different flags are AND'd:
  --ta respiratory --org Pfizer,AbbVie --phase 3
  = respiratory AND (Pfizer OR AbbVie) AND phase 3

[bold]Endpoint filters[/bold] (stats), comma-separated and OR'd the same way:
  --measurement fev1,fvc   --form change_from_baseline   --timepoint single_fixed   --scale litres

Filters that match no pulled study print a notice and exit cleanly. `conform` and
`results conform` always rebuild over the whole warehouse and take no filters.
Valid ids: vocab/therapeutic_areas.yaml, vocab/drug_classes.yaml
(or `endpoints drug-class distribution --top 0`).
"""

STATS = """\
[bold]endpoints stats[/bold] -- arm-level variability, grouped by form and unit

  uv run endpoints stats --measurement fev1
  uv run endpoints stats --measurement fev1 --form change_from_baseline --scale litres
  uv run endpoints stats --measurement fev1 --ta respiratory --phase 3
  uv run endpoints stats --measurement fev1 --org "GlaxoSmithKline,AstraZeneca" --since 2015-01-01
  uv run endpoints stats --measurement fev1 --drug-class muscarinic_antagonist

[bold]Stratify[/bold]
  uv run endpoints stats --measurement fev1 --by drug-class
  uv run endpoints stats --measurement fev1 --by drug-class --form change_from_baseline --ta respiratory --org Pfizer
  uv run endpoints stats --measurement fev1 --by drug-class --drug-class muscarinic_antagonist,beta2_agonist
  uv run endpoints stats --measurement fev1 --by drug-class --by-kind modality

[bold]Other views[/bold]
  uv run endpoints stats --measurement fev1 --analyses     [dim]# effect sizes, p-values, NI margins[/dim]
  uv run endpoints stats --measurement fev1 --source baseline
  uv run endpoints stats --measurement fev1 --only-reported --no-approximate
  uv run endpoints stats --measurement fev1 --json

Requires `endpoints results conform`. All filters: `endpoints help filters`.
"""

PULL = """\
[bold]endpoints pull[/bold] -- land studies into raw.* (upserts by default)

  uv run endpoints pull --phase 3 --limit 500
  uv run endpoints pull --phase 2,3 --since 2023-01-01 --limit 500
  uv run endpoints pull --phase 3 --ta oncology,cardiovascular
  uv run endpoints pull --phase 3 --org "Pfizer,AbbVie"
  uv run endpoints pull --phase 3 --drug-class glp1_receptor_agonist
  uv run endpoints pull --phase 3 --source aact            [dim]# needs .env credentials[/dim]
  uv run endpoints pull --phase 3 --no-results             [dim]# smaller warehouse[/dim]
  uv run endpoints pull --phase 3 --org Pfizer --replace   [dim]# discard earlier pulls[/dim]

--phase is required here. Every pull resolves therapeutic areas and drug classes.
"""

DRUG_CLASS = """\
[bold]endpoints drug-class[/bold]

  uv run endpoints drug-class distribution
  uv run endpoints drug-class distribution --kind mechanism --primary-only --top 0
  uv run endpoints drug-class distribution --ta oncology --org Merck
  uv run endpoints drug-class coverage --phase 3 --since 2020-01-01
  uv run endpoints drug-class diff-ancestors --out drug_class_ancestor_diff.csv
"""

RESULTS = """\
[bold]endpoints results[/bold]

  uv run endpoints results conform                  [dim]# run `endpoints conform` first[/dim]
  uv run endpoints results coverage
  uv run endpoints results coverage --ta respiratory --phase 3 --top 10
"""

USDM = """\
[bold]endpoints usdm[/bold]

  uv run endpoints usdm show NCT04162249
  uv run endpoints usdm show NCT04162249 --envelope wrapper -o study.json
  uv run endpoints usdm show NCT04162249 --level primary --flatten
  uv run endpoints usdm show NCT04162249 --tier templated
  uv run endpoints usdm coverage
  uv run endpoints usdm coverage --ta oncology --org Pfizer --limit 100
"""

VOCAB = """\
[bold]endpoints vocab[/bold]

  uv run endpoints vocab validate                   [dim]# check, then write vocab.* tables[/dim]
  uv run endpoints vocab validate --check-only
  uv run endpoints vocab validate --strict
  uv run endpoints vocab sample
  uv run endpoints vocab sample --format rows --limit 1000
  uv run endpoints vocab sample --outcome-type primary --ta respiratory
"""

REVIEW = """\
[bold]endpoints review[/bold]

  uv run endpoints review list
  uv run endpoints review list --reason measurement_unmatched --limit 50
  uv run endpoints review list --ta oncology --org Pfizer
"""

TA = """\
[bold]endpoints ta[/bold]

  uv run endpoints ta diff-tree --out ta_tree_diff.csv
  uv run endpoints ta diff-tree --phase 3 --since 2020-01-01
"""

TOPICS = {
    "filters": FILTERS,
    "stats": STATS,
    "pull": PULL,
    "drug-class": DRUG_CLASS,
    "results": RESULTS,
    "usdm": USDM,
    "vocab": VOCAB,
    "review": REVIEW,
    "ta": TA,
}


def render(topic: str | None) -> str | None:
    """The text for `topic` (the overview when None), or None if unknown."""
    if topic is None:
        return OVERVIEW.format(topics=", ".join(TOPICS))
    return TOPICS.get(topic.strip().lower())
