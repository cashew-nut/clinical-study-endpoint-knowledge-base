"""`endpoints` CLI (plan §7). Only `pull` is implemented in build-order step 1;
the rest of the interface is stubbed so `--help` reflects the full intended
shape, and each stub names the step that implements it.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from clinical_endpoints.conform.pipeline import run_conform
from clinical_endpoints.db import (
    AactConnectionError,
    MissingAactCredentialsError,
    attach_aact,
    connect,
)
from clinical_endpoints.ingest import aact as aact_backend
from clinical_endpoints.ingest import ctgov_api as ctgov_api_backend
from clinical_endpoints.ingest.ctgov_api import CtgovApiError
from clinical_endpoints.ingest.filters import PullFilters
from clinical_endpoints.ingest.upsert import SchemaMigrationError
from clinical_endpoints.results import coverage as results_coverage
from clinical_endpoints.results.pipeline import NoResults, run_results_conform
from clinical_endpoints.results.stats import (
    NotComputed,
    StatsFilters,
    analysis_distribution,
    sd_distribution,
    stratify_by_drug_class,
)
from clinical_endpoints.usdm.codes import UnknownOutcomeType
from clinical_endpoints.usdm.envelope import module_envelope, wrapper_envelope
from clinical_endpoints.usdm.project import (
    NotConformed,
    NotPulled,
    load_projection_rules,
    project,
)
from clinical_endpoints.vocab.loader import (
    VocabError,
    default_vocab_dir,
    load_vocab,
    validate_vocab,
    write_vocab_tables,
)
from clinical_endpoints.vocab.sample import OUTCOME_TYPES, run_vocab_sample
from clinical_endpoints.ta.resolver import (
    diff_tree_vs_pattern,
    filter_raw_tables_by_nct_ids,
    run_ta_resolution,
    tree_availability_summary,
)
from clinical_endpoints.drug_class.resolver import (
    coverage_summary as drug_class_coverage_summary,
    diff_ancestors,
    run_drug_class_resolution,
)

SOURCES = ("aact", "ctgov_api")

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

vocab_app = typer.Typer(no_args_is_help=True, help="Vocabulary sampling / validation.")
review_app = typer.Typer(no_args_is_help=True, help="Review-queue management.")
ta_app = typer.Typer(no_args_is_help=True, help="Therapeutic-area mapping.")
drug_class_app = typer.Typer(
    no_args_is_help=True, help="Drug-class resolution: distribution, coverage, and the ancestor diff."
)
usdm_app = typer.Typer(no_args_is_help=True, help="CDISC USDM 4.0 projection.")
results_app = typer.Typer(
    no_args_is_help=True, help="The results section: conforming and coverage."
)
app.add_typer(vocab_app, name="vocab")
app.add_typer(review_app, name="review")
app.add_typer(ta_app, name="ta")
app.add_typer(drug_class_app, name="drug-class")
app.add_typer(usdm_app, name="usdm")
app.add_typer(results_app, name="results")

USDM_ENVELOPES = ("module", "wrapper")

#: What `stats --by` accepts. One entry today; the option exists rather than a
#: bare `--by-drug-class` flag because the *next* stratifier (phase, TA, calendar
#: era) is the same shape and should not need a new flag.
STATS_STRATIFIERS = ("drug-class",)


def _determinate_progress() -> Progress:
    """A progress bar for work with a known total (row/step counts). Disabled
    outright when stdout isn't a terminal (e.g. under the test runner), so it
    never litters captured output with redraws."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
        disable=not console.is_terminal,
    )


def _indeterminate_progress() -> Progress:
    """A progress bar for work with no knowable total up front (paginating an
    API until it stops handing back pages)."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
        disable=not console.is_terminal,
    )


def _not_yet_implemented(command: str, instead: str) -> None:
    console.print(
        f"[yellow]`{command}` is not implemented -- {instead}. "
        "See docs/USAGE.md, \"Not implemented\".[/yellow]"
    )
    raise typer.Exit(code=1)


def _table_exists(con, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


def _vocab_drug_class_tables_ready(con) -> bool:
    """Whether `vocab validate` has loaded the drug-class mapping into this
    warehouse. Checked separately from the TA tables because a warehouse
    validated by an older release has the TA tables and not these, and the pull
    should degrade to skipping drug classes rather than failing."""
    return all(
        _table_exists(con, "vocab", table)
        for table in ("drug_classes", "drug_class_agent_names", "drug_class_name_patterns")
    )


def _vocab_ta_tables_ready(con) -> bool:
    """Whether `vocab validate` has already loaded the TA mapping into this
    warehouse -- the TA resolver reads vocab.ta_mesh_* tables, not the YAML
    directly (except for the two `defaults`, which aren't persisted at all)."""
    return _table_exists(con, "vocab", "ta_mesh_term_overrides") and _table_exists(
        con, "vocab", "therapeutic_areas"
    )


def _ensure_vocab_loaded(
    con, *, vocab_dir: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """Load `vocab/*.yaml` into this warehouse if it does not already hold a
    vocabulary the resolvers can read. Returns `(message, warning)`, at most one
    of which is set; never raises, and never aborts the pull.

    Therapeutic-area and drug-class resolution live inside `pull` because both
    are functions of what the pull just landed. They read `vocab.*` tables, so a
    warehouse pulled into *before* `vocab validate` ran used to land the raw
    conditions and interventions and then skip both derived tables -- and
    because resolution runs nowhere else, the only way to fill them in
    afterwards was **another network pull**. The data had already arrived; only
    the classification of it was missing. Loading the vocabulary here costs no
    network and makes a single `endpoints pull` on an empty directory produce a
    complete warehouse whatever order the operator ran things in.

    Narrow on purpose: it fires only when an axis's tables are *absent*, so a
    complete snapshot pinned by an earlier `vocab validate` (or by `--vocab-dir`
    against an edited copy) is left exactly as it is, and an edit under `vocab/`
    still does not take effect until you re-validate. The one case where it
    rewrites rather than adds is a warehouse whose vocabulary predates an axis
    entirely -- there the held snapshot has no answer to give, and the message
    says the reload happened. The project's "every run is against a validated
    snapshot" contract is kept rather than bypassed: what this writes is
    validated by exactly the checks `vocab validate` runs, and refuses to write
    anything if they fail.
    """
    ta_ready = _vocab_ta_tables_ready(con)
    class_ready = _vocab_drug_class_tables_ready(con)
    if ta_ready and class_ready:
        return None, None
    # A warehouse validated by a release older than an axis has some of the
    # vocabulary and not the tables that axis needs. Reloading is right there --
    # the held snapshot cannot answer the question being asked of it -- but it
    # is a rewrite, not an addition, so the message below says which happened.
    stale = ta_ready or class_ready

    try:
        resolved = Path(vocab_dir) if vocab_dir else default_vocab_dir()
        docs = load_vocab(resolved)
    except VocabError as exc:
        # No `vocab/` on disk (an installed wheel that does not package it, say).
        # The pull is unaffected; it just cannot classify what it landed.
        return None, (
            f"Could not load a vocabulary to resolve this pull against ({exc}). "
            "The studies landed; run `endpoints vocab validate --vocab-dir <path>` "
            "and re-run `pull` to classify them."
        )

    result = validate_vocab(docs)
    if result.errors:
        first = result.errors[0]
        more = f" (+{len(result.errors) - 1} more)" if len(result.errors) > 1 else ""
        return None, (
            f"The vocabulary in {resolved} does not validate, so nothing was loaded and "
            f"this pull could not be classified: {first}{more}. "
            "Run `endpoints vocab validate` to see all of them."
        )

    counts = write_vocab_tables(con, docs, vocab_dir=resolved)
    why = (
        "this warehouse's vocabulary predates the tables the resolvers need, so it was "
        "reloaded in full"
        if stale
        else "this warehouse had none"
    )
    return (
        f"Loaded the vocabulary from {resolved} first -- {why} "
        f"({sum(counts.values())} rows across {len(counts)} vocab.* tables). "
        "Edits under vocab/ still need `endpoints vocab validate` to take effect.",
        None,
    )


@app.command()
def pull(
    phase: str = typer.Option(
        ..., "--phase", help='Comma-separated phases, e.g. "3" or "1/2,2,3".'
    ),
    limit: int = typer.Option(500, "--limit", help="Max studies to pull, most recent first."),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only studies with start_date on/after this date (YYYY-MM-DD)."
    ),
    ta: Optional[str] = typer.Option(
        None,
        "--ta",
        help="Therapeutic-area filter (comma-separated ids from therapeutic_areas.yaml, "
        "e.g. oncology,respiratory). Filters against the mapping loaded into vocab.*; "
        "if this warehouse holds none, `pull` loads one first.",
    ),
    drug_class: Optional[str] = typer.Option(
        None,
        "--drug-class",
        help="Drug-class filter (comma-separated ids from drug_classes.yaml, e.g. "
        "glp1_receptor_agonist,sglt2_inhibitor). Applied client-side before --limit, "
        "same as --ta -- neither backend can express it server-side. NOT needed to get "
        "drug classes: every pull resolves them for whatever it lands.",
    ),
    org: Optional[str] = typer.Option(
        None,
        "--org",
        help="Organisation filter (comma-separated name fragments, matched case-insensitively "
        'against the *lead* sponsor only, e.g. "Pfizer" or "Pfizer,AbbVie"). Applied '
        "server-side before --limit, same as --phase/--since; no `vocab validate` precondition.",
    ),
    results: bool = typer.Option(
        True,
        "--results/--no-results",
        help="Land the results section (raw.outcome_measures / outcome_groups / "
        "outcome_measurements / outcome_analyses / baseline_measurements) for studies "
        "that posted one. On by default: the CT.gov API returns it in the payload the "
        "pull already fetches, so --no-results saves warehouse size, never network.",
    ),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Replace raw.* with just this pull's results instead of upserting -- discards "
        "studies landed by any earlier pull, including ones with different filters or a "
        "different --source. Default is to upsert (accumulate); use this to make the "
        "warehouse mirror exactly this pull.",
    ),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
    source: str = typer.Option(
        "ctgov_api",
        "--source",
        help='Ingestion backend: "ctgov_api" (public API, default -- AACT access is '
        'currently broken) or "aact" (requires .env credentials).',
    ),
) -> None:
    """Pull filtered studies + design_outcomes/conditions/interventions into raw.*,
    log the pull, and resolve therapeutic areas into
    conformed.study_therapeutic_area and drug classes into
    conformed.study_drug_class / arm_drug_class -- filtering down to `--ta`,
    `--drug-class` and/or `--org` if given.

    One pull, one wave: everything a study contributes is in the payload this
    already fetches, so there is no second pull per axis. A warehouse holding no
    vocabulary gets one loaded first, so a first `pull` into an empty directory
    is complete on its own. Upserts by default; `--replace` discards everything
    raw.* already held instead."""
    if source not in SOURCES:
        console.print(f"[red]--source must be one of {SOURCES}, got {source!r}[/red]")
        raise typer.Exit(code=1)

    ta_ids: Optional[tuple] = None
    if ta:
        ta_ids = tuple(t.strip() for t in ta.split(",") if t.strip())

    org_ids: Optional[tuple] = None
    if org:
        org_ids = tuple(o.strip() for o in org.split(",") if o.strip())

    class_ids: Optional[tuple] = None
    if drug_class:
        class_ids = tuple(c.strip() for c in drug_class.split(",") if c.strip())

    since_date: Optional[dt.date] = None
    if since:
        try:
            since_date = dt.date.fromisoformat(since)
        except ValueError as exc:
            console.print(f"[red]--since must be YYYY-MM-DD, got {since!r}[/red]")
            raise typer.Exit(code=1) from exc

    phases = tuple(p.strip() for p in phase.split(",") if p.strip())
    filters = PullFilters(
        phases=phases, limit=limit, since=since_date, ta=ta_ids, org=org_ids,
        drug_class=class_ids, replace=replace, with_results=results,
    )

    con = connect(warehouse)
    try:
        # Before anything else, so that a first `pull` into an empty directory
        # resolves what it lands instead of leaving the derived tables for a
        # second, network-costing pull -- and so --ta/--drug-class below are
        # answerable on a warehouse that has never been validated into.
        vocab_message, vocab_warning = _ensure_vocab_loaded(con)
        if vocab_message:
            console.print(f"[green]{vocab_message}[/green]")
        if vocab_warning:
            console.print(f"[yellow]{vocab_warning}[/yellow]")

        if ta_ids and not _vocab_ta_tables_ready(con):
            console.print(
                "[red]--ta requires the MeSH->TA vocabulary to already be loaded into this "
                "warehouse: run `endpoints vocab validate` first.[/red]"
            )
            raise typer.Exit(code=1)

        if ta_ids:
            known_tas = {
                row[0] for row in con.execute("SELECT id FROM vocab.therapeutic_areas").fetchall()
            }
            unknown = sorted(set(ta_ids) - known_tas)
            if unknown:
                console.print(
                    f"[red]--ta names unknown therapeutic area(s) {unknown}. "
                    f"Valid ids: {sorted(known_tas)}[/red]"
                )
                raise typer.Exit(code=1)

        if class_ids:
            if not _vocab_drug_class_tables_ready(con):
                console.print(
                    "[red]--drug-class requires the drug-class vocabulary to already be loaded "
                    "into this warehouse: run `endpoints vocab validate` first.[/red]"
                )
                raise typer.Exit(code=1)
            known_classes = {
                row[0] for row in con.execute("SELECT id FROM vocab.drug_classes").fetchall()
            }
            unknown = sorted(set(class_ids) - known_classes)
            if unknown:
                console.print(
                    f"[red]--drug-class names unknown drug class(es) {unknown}. "
                    f"See `endpoints drug-class distribution` or vocab/drug_classes.yaml "
                    f"for the {len(known_classes)} valid ids.[/red]"
                )
                raise typer.Exit(code=1)

        if source == "aact":
            try:
                attach_aact(con)
            except (MissingAactCredentialsError, AactConnectionError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            with _determinate_progress() as progress:
                task = progress.add_task("Pulling from AACT...", total=len(aact_backend.PULL_STEPS))

                def on_step(step_name: str, index: int, total: int) -> None:
                    progress.update(task, completed=index, description=f"Pulling from AACT: {step_name}")

                result = aact_backend.run_pull(con, filters, on_step=on_step)
        else:
            try:
                with _indeterminate_progress() as progress:
                    task = progress.add_task("Pulling from ClinicalTrials.gov API...", total=None)

                    def on_page(page_index: int, studies_collected: int) -> None:
                        progress.update(
                            task,
                            description=(
                                f"Pulling from ClinicalTrials.gov API: page {page_index}, "
                                f"{studies_collected} studies collected"
                            ),
                        )

                    result = ctgov_api_backend.run_pull(con, filters, on_page=on_page)
            except CtgovApiError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc

        ta_summary = None
        if _vocab_ta_tables_ready(con):
            ta_summary = run_ta_resolution(con)
            if ta_ids:
                keep = {
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT nct_id FROM conformed.study_therapeutic_area WHERE ta_id = ANY(?)",
                        [list(ta_ids)],
                    ).fetchall()
                }
                result["row_counts"] = filter_raw_tables_by_nct_ids(con, set(result["nct_ids"]), keep)
                ta_summary = run_ta_resolution(con)  # re-derive the distribution for the kept studies only

        drug_class_summary = None
        if _vocab_drug_class_tables_ready(con):
            drug_class_summary = run_drug_class_resolution(con)
            if class_ids:
                # The same post-filter shape `--ta` uses: the backends filter
                # while scanning, and this catches anything their (necessarily
                # partial, on AACT) view of a candidate missed.
                keep = {
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT nct_id FROM conformed.study_drug_class "
                        "WHERE drug_class_id = ANY(?)",
                        [list(class_ids)],
                    ).fetchall()
                }
                result["row_counts"] = filter_raw_tables_by_nct_ids(con, set(result["nct_ids"]), keep)
                drug_class_summary = run_drug_class_resolution(con)
                if ta_summary is not None:
                    ta_summary = run_ta_resolution(con)
    except SchemaMigrationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        con.close()

    # --replace discards rather than migrates, so it bypasses the reconciler's
    # migration path entirely (see ingest/upsert.py's `ensure_table`) -- report
    # it here, once, the way a migration is reported per table below.
    if replace:
        console.print(
            "[yellow]--replace: raw.* tables were replaced, not upserted -- studies from any "
            "earlier pull (different filters, different --source) are gone.[/yellow]"
        )

    # A migration rewrites tables the operator already had; say so rather than
    # letting rows quietly change shape underneath them.
    migrations = result.get("migrations", [])
    for change in migrations:
        console.print(f"[yellow]Migrated {change.describe()}[/yellow]")
    if any(change.added and change.rows_kept for change in migrations):
        # Migration backfills NULL, not data: the new columns are only populated
        # for studies this pull actually touched.
        console.print(
            "[yellow]New columns are NULL for studies landed by earlier pulls -- "
            "re-run `pull` covering them to fill in.[/yellow]"
        )

    console.print(
        f"[green]Pull {result['pull_id']} complete[/green] "
        f"(source={source}, {result['pulled_at'].isoformat()}): "
        f"{result['row_counts']['studies']} studies, "
        f"{result['row_counts']['design_outcomes']} design_outcomes -> raw.*"
    )

    if result.get("results_warning"):
        console.print(f"[yellow]{result['results_warning']}[/yellow]")
    elif results:
        counts = result["row_counts"]
        landed = counts.get("outcome_measures", 0)
        if landed:
            console.print(
                f"[green]Results section: {landed} reported outcomes, "
                f"{counts.get('outcome_measurements', 0)} arm-level measurements, "
                f"{counts.get('outcome_analyses', 0)} analyses, "
                f"{counts.get('baseline_measurements', 0)} baseline rows -> raw.outcome_*[/green]"
            )
        else:
            console.print(
                "[yellow]No results section landed -- none of the studies in this pull has "
                "posted results. `endpoints results coverage` reports the denominator.[/yellow]"
            )
    if org_ids:
        console.print(f"[green]Filtered to --org {list(org_ids)}[/green]")
    if ta_ids:
        console.print(f"[green]Filtered to --ta {list(ta_ids)}[/green]")
        if result.get("hit_scan_cap"):
            console.print(
                f"[yellow]Only found {result['row_counts']['studies']} of the requested "
                f"{limit} studies matching --ta {list(ta_ids)} after scanning "
                f"{result.get('studies_scanned', '?')} studies -- ClinicalTrials.gov has no "
                "server-side filter for this project's therapeutic areas, so `pull` scans "
                "recent studies broadly and keeps only matches. Narrow --since or accept "
                "the smaller result.[/yellow]"
            )
    if ta_summary is not None:
        dist = ", ".join(f"{ta_id}={n}" for ta_id, n in ta_summary["distribution"].items())
        console.print(
            f"Therapeutic areas resolved for {ta_summary['study_count']} studies -> "
            f"conformed.study_therapeutic_area ({dist})"
        )
    elif not ta_ids:
        console.print(
            "[yellow]Skipped therapeutic-area resolution -- this warehouse has no "
            "vocabulary and none could be loaded (see above). conformed.study_therapeutic_area "
            "was not written.[/yellow]"
        )

    if result.get("interventions_warning"):
        console.print(f"[yellow]{result['interventions_warning']}[/yellow]")
    if class_ids:
        console.print(f"[green]Filtered to --drug-class {list(class_ids)}[/green]")
    if drug_class_summary is not None:
        top = list(drug_class_summary["distribution"].items())[:6]
        dist = ", ".join(f"{class_id}={n}" for class_id, n in top)
        console.print(
            f"Drug classes resolved for {drug_class_summary['study_count']} studies "
            f"({drug_class_summary['classified_studies']} classified) -> "
            f"conformed.study_drug_class ({dist})"
        )
        if drug_class_summary["review_count"]:
            console.print(
                f"[yellow]{drug_class_summary['review_count']} intervention(s) matched no "
                "class -> conformed.drug_class_review_queue. `endpoints drug-class coverage` "
                "reports the denominator.[/yellow]"
            )
    elif not class_ids:
        console.print(
            "[yellow]Skipped drug-class resolution -- this warehouse has no vocabulary "
            "and none could be loaded (see above). conformed.study_drug_class was not "
            "written.[/yellow]"
        )


@vocab_app.command("sample")
def vocab_sample(
    min_frequency: int = typer.Option(
        2, "--min-frequency", help="Keep every distinct value occurring >= N times, uncapped."
    ),
    singleton_sample: int = typer.Option(
        300,
        "--singleton-sample",
        help="Random seeded sample size for values below --min-frequency (the long tail).",
    ),
    seed: int = typer.Option(
        42, "--seed", help="Seed for the singleton/row sample, so re-running is reproducible."
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        help="Hard cap per field (frequency format) or on rows written (rows format). 0 = unlimited.",
    ),
    fmt: str = typer.Option(
        "frequency", "--format", help='Export shape: "frequency" (default) or "rows".'
    ),
    outcome_type: Optional[str] = typer.Option(
        None,
        "--outcome-type",
        help=f'Comma-separated outcome types to include, from {OUTCOME_TYPES} (default: all).',
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="CSV output path (default: vocab_review.csv, or vocab_review_rows.csv for --format rows).",
    ),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """Export design_outcomes vocabulary for human review: a per-field frequency
    table with coverage reporting (default), or a joinable row-level sample
    (--format rows)."""
    if fmt not in ("frequency", "rows"):
        console.print(f"[red]--format must be 'frequency' or 'rows', got {fmt!r}[/red]")
        raise typer.Exit(code=1)

    outcome_types: Optional[tuple] = None
    if outcome_type:
        outcome_types = tuple(t.strip() for t in outcome_type.split(",") if t.strip())
        invalid = sorted(set(outcome_types) - set(OUTCOME_TYPES))
        if invalid:
            console.print(
                f"[red]--outcome-type values must be from {OUTCOME_TYPES}, got {invalid}[/red]"
            )
            raise typer.Exit(code=1)

    out_path = out or ("vocab_review_rows.csv" if fmt == "rows" else "vocab_review.csv")

    con = connect(warehouse)
    try:
        result = run_vocab_sample(
            con,
            min_frequency=min_frequency,
            singleton_sample=singleton_sample,
            seed=seed,
            limit=limit,
            out_path=out_path,
            fmt=fmt,
            outcome_types=outcome_types,
        )
    finally:
        con.close()

    if fmt == "rows":
        console.print(
            f"[green]Wrote {result['row_count']} rows -> {result['out_path']}[/green] "
            f"(seed={seed}, limit={limit or 'unlimited'})"
        )
        return

    counts = result["field_counts"]
    console.print(
        f"[green]Wrote {result['row_count']} rows -> {result['out_path']}[/green] "
        f"(measure={counts['measure']}, description={counts['description']}, "
        f"time_frame={counts['time_frame']})"
    )
    console.print(f"Coverage -> {result['coverage_path']}")

    table = Table("field", "distinct_total", "distinct_kept", "rows_total", "rows_covered", "pct_rows_covered")
    for row in result["coverage"]:
        table.add_row(
            row["field"],
            str(row["distinct_total"]),
            str(row["distinct_kept"]),
            str(row["rows_total"]),
            str(row["rows_covered"]),
            f"{row['pct_rows_covered']:.1f}",
        )
    console.print(table)


@vocab_app.command("validate")
def vocab_validate(
    vocab_dir: Optional[str] = typer.Option(
        None, "--vocab-dir", help="Path to the vocab/ directory (default: found by walking up from cwd)."
    ),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
    check_only: bool = typer.Option(
        False, "--check-only", help="Validate without writing to the warehouse."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Treat warnings as errors."
    ),
) -> None:
    """Load vocab/*.yaml into vocab.* tables; check ids, synonyms, regexes, cross-file refs."""
    try:
        resolved = Path(vocab_dir) if vocab_dir else default_vocab_dir()
        docs = load_vocab(resolved)
    except VocabError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    result = validate_vocab(docs)

    for warning in result.warnings:
        console.print(f"[yellow]warning[/yellow]  {warning}")
    for error in result.errors:
        console.print(f"[red]error[/red]    {error}")

    if result.errors or (strict and result.warnings):
        console.print(
            f"[red]{len(result.errors)} error(s), {len(result.warnings)} warning(s) "
            f"in {resolved} -- nothing written.[/red]"
        )
        raise typer.Exit(code=1)

    summary = ", ".join(f"{dim}={n}" for dim, n in sorted(result.term_counts.items()))
    if check_only:
        console.print(f"[green]Vocabulary valid[/green] ({summary}); --check-only, nothing written.")
        return

    con = connect(warehouse)
    try:
        counts = write_vocab_tables(con, docs, vocab_dir=resolved)
    finally:
        con.close()

    console.print(f"[green]Vocabulary valid[/green] ({summary})")
    console.print(
        f"Wrote {sum(counts.values())} rows across {len(counts)} vocab.* tables -> {warehouse}"
        + (f" ({len(result.warnings)} warning(s))" if result.warnings else "")
    )


@ta_app.command("diff-tree")
def ta_diff_tree(
    out: str = typer.Option("ta_tree_diff.csv", "--out", help="CSV output path."),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """Run the tree-prefix layer alone and the regex layer alone over every
    pulled study's conditions, and report every disagreement, most frequent first --
    each one is either a wrong tree prefix or a wrong regex in ta_mesh_mapping.yaml.
    Requires `pull` and `vocab validate` to have already been run against this warehouse."""
    con = connect(warehouse)
    try:
        if not _vocab_ta_tables_ready(con):
            console.print(
                "[red]Run `endpoints vocab validate` first -- this needs the vocab.ta_mesh_* "
                "tables.[/red]"
            )
            raise typer.Exit(code=1)
        if not _table_exists(con, "raw", "browse_conditions"):
            console.print("[red]Run `endpoints pull` first -- no raw.browse_conditions yet.[/red]")
            raise typer.Exit(code=1)

        availability = tree_availability_summary(con)
        diffs = diff_tree_vs_pattern(con)
    finally:
        con.close()

    out_path = Path(out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("mesh_term", "tree_number", "ta_from_tree", "ta_from_pattern", "count"))
        for d in diffs:
            writer.writerow((d["mesh_term"], d["tree_number"], d["ta_from_tree"], d["ta_from_pattern"], d["count"]))

    console.print(
        f"[green]Wrote {len(diffs)} disagreement(s) -> {out_path}[/green] "
        f"({availability['total_condition_rows']} condition rows, "
        f"{availability['with_aact_tree_number']} with an AACT tree number, "
        f"{availability['studies_with_ctgov_branch']} studies with a CT.gov branch)"
    )
    if not diffs and availability["with_aact_tree_number"] == 0 and availability["studies_with_ctgov_branch"] == 0:
        console.print(
            "[yellow]No tree-number signal at all in this pull -- nothing to diff against the "
            "regex layer (see vocab/ta_mesh_mapping.yaml's caveats).[/yellow]"
        )


@drug_class_app.command("distribution")
def drug_class_distribution(
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        help="Only classes of this kind: mechanism, pharmacologic, modality or control. "
        "Mixing kinds in one list compares a mechanism against a modality as if they "
        "were alternatives, so this is usually what you want.",
    ),
    primary_only: bool = typer.Option(
        False,
        "--primary-only",
        help="Count each study once, under its primary class, instead of counting every "
        "class it matched. Combination therapy is the norm, so the two differ a lot.",
    ),
    top: int = typer.Option(30, "--top", help="How many classes to list; 0 = all."),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """What the pulled corpus is made of, by drug class.

    Requires `pull` and `vocab validate` to have been run against this warehouse."""
    con = connect(warehouse)
    try:
        if not _table_exists(con, "conformed", "study_drug_class"):
            console.print(
                "[red]No conformed.study_drug_class yet -- run `endpoints vocab validate` "
                "and then `endpoints pull`.[/red]"
            )
            raise typer.Exit(code=1)

        where = ["1 = 1"]
        params: list = []
        if kind:
            where.append("c.kind = ?")
            params.append(kind)
        if primary_only:
            where.append("c.is_primary")
        limit_clause = "" if top <= 0 else f"LIMIT {int(top)}"
        rows = con.execute(
            f"""
            SELECT c.drug_class_id, c.kind, count(DISTINCT c.nct_id) AS studies,
                   count(DISTINCT CASE WHEN c.is_primary THEN c.nct_id END) AS primary_studies,
                   count(DISTINCT c.rule_layer) AS layers
            FROM conformed.study_drug_class c
            WHERE {" AND ".join(where)}
            GROUP BY 1, 2 ORDER BY studies DESC, c.drug_class_id
            {limit_clause}
            """,
            params,
        ).fetchall()
        coverage = drug_class_coverage_summary(con)
    finally:
        con.close()

    if not rows:
        console.print("[yellow]No drug classes matched that filter.[/yellow]")
        return

    table = Table(title="Drug classes in this warehouse")
    table.add_column("class")
    table.add_column("kind")
    table.add_column("studies", justify="right")
    table.add_column("as primary", justify="right")
    for class_id, class_kind, studies, primary_studies, _layers in rows:
        table.add_row(class_id, class_kind, f"{studies:,}", f"{primary_studies:,}")
    console.print(table)

    # The denominator, always: a class list without it is a machine for making a
    # thin axis look complete (docs/DRUG_CLASS_SPEC.md).
    console.print(
        f"{coverage['classified_studies']:,} of {coverage['studies']:,} studies carry at least "
        f"one named class ({_pct(coverage['classified_studies'], coverage['studies'])}); "
        f"{coverage['mechanism_studies']:,} carry a mechanism class "
        f"({_pct(coverage['mechanism_studies'], coverage['studies'])})."
    )
    if not coverage["ancestor_studies"]:
        console.print(
            "[yellow]No MeSH intervention ancestors in this warehouse, so the mechanism layer "
            "rested on curated names and INN stems alone. Expected on an AACT pull, which "
            "publishes no ancestry; on a CT.gov pull it means the API returned none.[/yellow]"
        )
    if coverage["review_queue"]:
        console.print(
            f"[yellow]{coverage['review_queue']:,} intervention(s) in "
            "conformed.drug_class_review_queue matched no class at all.[/yellow]"
        )


@drug_class_app.command("coverage")
def drug_class_coverage_cmd(
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
    top: int = typer.Option(20, "--top", help="How many unclassified agents to list."),
) -> None:
    """How much of the corpus the drug-class axis actually covers, and what it missed.

    The honest half of `distribution`: the review queue is the input to the next
    vocabulary round, exactly as `endpoints review list` is on the endpoint side."""
    con = connect(warehouse)
    try:
        coverage = drug_class_coverage_summary(con)
        unclassified = []
        if _table_exists(con, "conformed", "drug_class_review_queue"):
            unclassified = con.execute(
                """
                SELECT name, count(*) AS n FROM conformed.drug_class_review_queue
                GROUP BY 1 ORDER BY n DESC, name LIMIT ?
                """,
                [max(top, 0)],
            ).fetchall()
    finally:
        con.close()

    if not coverage.get("resolved"):
        console.print(
            "[red]No conformed.study_drug_class yet -- run `endpoints vocab validate` "
            "and then `endpoints pull`.[/red]"
        )
        raise typer.Exit(code=1)

    studies = coverage["studies"]
    table = Table(title="Drug-class coverage")
    table.add_column("measure")
    table.add_column("studies", justify="right")
    table.add_column("of corpus", justify="right")
    for label, value in (
        ("pulled", studies),
        ("with >=1 registered intervention", coverage["studies_with_interventions"]),
        ("with >=1 named class", coverage["classified_studies"]),
        ("with a mechanism class", coverage["mechanism_studies"]),
        ("with a control arm named", coverage["control_studies"]),
        ("with MeSH ancestors (CT.gov only)", coverage["ancestor_studies"]),
        ("with browse branches (CT.gov only)", coverage["branch_studies"]),
    ):
        table.add_row(label, f"{value:,}", _pct(value, studies))
    console.print(table)
    console.print(
        f"{coverage['arm_rows']:,} arm-level class rows in conformed.arm_drug_class; "
        f"{coverage['review_queue']:,} intervention(s) in the review queue."
    )

    if unclassified:
        queue = Table(title="Most frequent unclassified interventions")
        queue.add_column("intervention")
        queue.add_column("studies", justify="right")
        for name, n in unclassified:
            queue.add_row(name or "(no name)", f"{n:,}")
        console.print(queue)
        console.print(
            "These are the input to the next vocabulary round. Add the ones that recur to "
            "vocab/drug_class_mesh_mapping.yaml's `agent_names`; do not loosen a pattern to "
            "absorb them."
        )


@drug_class_app.command("diff-ancestors")
def drug_class_diff_ancestors(
    out: str = typer.Option("drug_class_ancestor_diff.csv", "--out", help="CSV output path."),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """Run the curated layers alone and NLM's MeSH ancestry alone over every
    pulled study's interventions, and report every disagreement, most frequent
    first -- each one is either a wrong `agent_names`/`name_patterns` entry or a
    wrong `ancestor_rules` entry in drug_class_mesh_mapping.yaml.

    It reports; it does not reconcile. Editing the YAML until the diff is empty
    destroys the only external check this axis has."""
    con = connect(warehouse)
    try:
        if not _vocab_drug_class_tables_ready(con):
            console.print(
                "[red]Run `endpoints vocab validate` first -- this needs the "
                "vocab.drug_class_* tables.[/red]"
            )
            raise typer.Exit(code=1)
        if not _table_exists(con, "raw", "interventions"):
            console.print("[red]Run `endpoints pull` first -- no raw.interventions yet.[/red]")
            raise typer.Exit(code=1)
        diffs = diff_ancestors(con)
        coverage = drug_class_coverage_summary(con)
    finally:
        con.close()

    out_path = Path(out)
    columns = (
        "intervention", "kind", "class_from_curated", "curated_layer",
        "class_from_ancestor", "ancestor_term", "count",
    )
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for d in diffs:
            writer.writerow(tuple(d[c] for c in columns))

    console.print(
        f"[green]Wrote {len(diffs)} disagreement(s) -> {out_path}[/green] "
        f"({coverage['ancestor_studies']:,} of {coverage['studies']:,} studies have any "
        "MeSH intervention ancestry to diff against)"
    )
    if not coverage["ancestor_studies"]:
        console.print(
            "[yellow]No intervention ancestors in this warehouse, so there was nothing to "
            "diff. AACT publishes none; on a CT.gov pull this means the API returned no "
            "interventionBrowseModule.ancestors[] (see vocab/drug_class_mesh_mapping.yaml's "
            "caveats).[/yellow]"
        )


@app.command()
def conform(
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
    jobs: int = typer.Option(
        0,
        "--jobs",
        "-j",
        help="Worker processes for the row-conforming step. 0 (default) = auto: parallelize "
        "across all CPUs once there's enough work to be worth it, serial otherwise. "
        "1 forces serial.",
    ),
) -> None:
    """Normalize -> syntactic rules -> semantic fallback -> review queue.
    Reads raw.design_outcomes + vocab.*, writes conformed.endpoints and
    conformed.review_queue. Requires `endpoints vocab validate` and `endpoints
    pull` to have already been run against this warehouse."""
    con = connect(warehouse)
    try:
        try:
            with _determinate_progress() as progress:
                task = progress.add_task("Conforming design_outcomes...", total=None)

                def on_progress(done: int, total: int) -> None:
                    progress.update(task, completed=done, total=total)

                result = run_conform(con, jobs=jobs, on_progress=on_progress)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        con.close()

    workers_note = f" ({result['workers']} worker processes)" if result.get("workers", 1) > 1 else ""
    console.print(
        f"[green]Conformed {result['rows_conformed']} of {result['total_rows']} row(s)[/green] "
        f"-> conformed.endpoints; {result['rows_queued']} -> conformed.review_queue{workers_note}"
    )


@usdm_app.command("show")
def usdm_show(
    nct_id: str = typer.Argument(..., help="The trial to project, e.g. NCT04162249."),
    envelope: str = typer.Option(
        "module", "--envelope", help="module (the endpoints module) or wrapper (a full USDM Wrapper)."
    ),
    flatten: bool = typer.Option(
        False, "--flatten", help="Return a flat endpoints[] instead of objectives[].endpoints[]."
    ),
    level: Optional[str] = typer.Option(
        None, "--level", help="Comma-separated: primary, secondary, exploratory."
    ),
    tier: Optional[str] = typer.Option(
        None, "--tier", help="Comma-separated: templated, partial, verbatim."
    ),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Write JSON here instead of stdout."),
    warehouse: str = typer.Option("warehouse.duckdb", "--warehouse"),
) -> None:
    """A USDM 4.0 representation of every endpoint in one trial.

    Each endpoint's `text` is a syntax template whose tags resolve through its
    `SyntaxTemplateDictionary` into the controlled vocabularies; the registry
    string is kept verbatim in `description`, so the projection is auditable.
    """
    if envelope not in USDM_ENVELOPES:
        console.print(f"[red]--envelope must be one of {', '.join(USDM_ENVELOPES)}[/red]")
        raise typer.Exit(code=2)

    con = connect(warehouse)
    try:
        try:
            rules = load_projection_rules(con)
            projection = project(
                con,
                nct_id,
                rules=rules,
                levels=_split_option(level),
                tiers=_split_option(tier),
            )
        except NotPulled as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except NotConformed as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except UnknownOutcomeType as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        if envelope == "wrapper":
            body = wrapper_envelope(con, projection, vocab_version=rules.vocab_version)
        else:
            body = module_envelope(
                con, projection, vocab_version=rules.vocab_version, flatten=flatten
            )
    finally:
        con.close()

    payload = json.dumps(body, indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(payload + "\n", encoding="utf-8")
        console.print(
            f"[green]{projection.endpoint_count} endpoint(s)[/green] -> {out} "
            f"({', '.join(f'{k} {v}' for k, v in sorted(projection.tiers.items()))})"
        )
    else:
        print(payload)


@usdm_app.command("coverage")
def usdm_coverage(
    warehouse: str = typer.Option("warehouse.duckdb", "--warehouse"),
    limit: int = typer.Option(0, "--limit", help="Only the first N trials; 0 = all."),
) -> None:
    """The fidelity-tier mix across every conformed trial.

    The tiers are the honest measure of what the templates buy: `templated` is a
    fully parameterized endpoint, `partial` dropped an optional group, and
    `verbatim` is the registry string passed through because no template
    applied. Per-dimension vocabulary coverage does not compose into this --
    the tiers depend on joint resolution -- so it has to be counted.
    """
    con = connect(warehouse)
    try:
        rules = load_projection_rules(con)
        nct_ids = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT nct_id FROM conformed.endpoints ORDER BY nct_id"
            ).fetchall()
        ]
        if limit:
            nct_ids = nct_ids[:limit]
        totals: dict[str, int] = {}
        defaulted_totals: dict[str, int] = {}
        for nct_id in nct_ids:
            projection = project(con, nct_id, rules=rules)
            for tier, count in projection.tiers.items():
                totals[tier] = totals.get(tier, 0) + count
            for tag, count in projection.defaulted.items():
                defaulted_totals[tag] = defaulted_totals.get(tag, 0) + count
    finally:
        con.close()

    grand = sum(totals.values())
    if not grand:
        console.print("[yellow]No conformed endpoints -- run `endpoints conform` first.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title=f"USDM fidelity tiers over {len(nct_ids)} trial(s)")
    table.add_column("tier")
    table.add_column("endpoints", justify="right")
    table.add_column("share", justify="right")
    for tier in ("templated", "partial", "verbatim"):
        count = totals.get(tier, 0)
        table.add_row(tier, str(count), f"{100 * count / grand:.1f}%")
    table.add_row("[bold]total", f"[bold]{grand}", "")
    console.print(table)

    # docs/USDM_PROJECTION_INTEGRITY_SPEC.md change 1: how much of the
    # templated tier is standing on an announced default, not a resolved
    # value -- a subset of "templated", never hidden inside it.
    if defaulted_totals:
        templated = totals.get("templated", 0) or 1
        parts = ", ".join(
            f"{tag} defaulted: {count} ({100 * count / templated:.1f}% of templated)"
            for tag, count in sorted(defaulted_totals.items())
        )
        console.print(f"[dim]{parts}[/dim]")


@results_app.command("conform")
def results_conform(
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """Conform the results section and normalise its dispersions.

    Reads raw.outcome_* and vocab.*, writes conformed.endpoint_results (one row
    per reported outcome and baseline characteristic, linked back to the
    planned endpoint where one can be identified),
    conformed.endpoint_dispersion (one row per arm-level measurement, with an
    `sd_estimate` and how it was derived) and conformed.results_review_queue.

    Requires `endpoints vocab validate` and `endpoints pull` (without
    --no-results); run `endpoints conform` first so results rows can be linked
    to their planned endpoints.
    """
    con = connect(warehouse)
    try:
        try:
            with _determinate_progress() as progress:
                task = progress.add_task("Conforming results...", total=None)

                def on_progress(done: int, total: int) -> None:
                    progress.update(task, completed=done, total=total)

                result = run_results_conform(con, on_progress=on_progress)
        except NoResults as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        con.close()

    console.print(
        f"[green]Conformed {result['rows_conformed']} of {result['results_rows']} results "
        f"row(s)[/green] -> conformed.endpoint_results; "
        f"{result['rows_queued']} -> conformed.results_review_queue"
    )
    if result["links"]:
        links = " · ".join(f"{method} {count}" for method, count in sorted(result["links"].items()))
        console.print(f"Links to planned endpoints: {links}")
    console.print(
        f"[green]{result['dispersion_with_sd']} of {result['dispersion_rows']} arm-level "
        f"measurement(s) yielded an SD estimate[/green] -> conformed.endpoint_dispersion"
    )
    if result["sd_methods"]:
        methods = " · ".join(f"{m} {c}" for m, c in result["sd_methods"].items())
        console.print(f"[dim]{methods}[/dim]")


def _pct(numerator, denominator) -> str:
    fraction = results_coverage.share(numerator, denominator)
    return "n/a" if fraction is None else f"{100 * fraction:.1f}%"


@results_app.command("coverage")
def results_coverage_cmd(
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
    top: int = typer.Option(20, "--top", help="How many distinct values to list per field."),
) -> None:
    """The four numbers the results tier was gated on, measured against this warehouse.

    What share of conformed studies posted results; what share of reported
    outcome titles match a planned one; the exact value sets `param_type` and
    `dispersion_type` use here, and which of them the vocabulary does not yet
    recognise; and what share of `unit_of_measure` strings normalise against
    scales.yaml. See docs/ENDPOINT_RESULTS_SPEC.md, "The gate".
    """
    con = connect(warehouse)
    try:
        try:
            report = results_coverage.gate_measurements(con, top=top)
        except results_coverage.NoResults as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        con.close()

    posting = report["posting"]
    console.print("[bold]1. Results posting[/bold]")
    console.print(
        f"  {posting['studies_flagged_has_results']} of {posting['studies_pulled']} pulled "
        f"studies are flagged hasResults ({_pct(posting['studies_flagged_has_results'], posting['studies_pulled'])})"
    )
    console.print(
        f"  {posting['studies_with_results_landed']} had a results section landed "
        f"({_pct(posting['studies_with_results_landed'], posting['studies_pulled'])} of pulled)"
    )
    if posting["studies_conformed"] is not None:
        console.print(
            f"  {posting['conformed_studies_with_results']} of {posting['studies_conformed']} "
            f"conformed studies have results "
            f"({_pct(posting['conformed_studies_with_results'], posting['studies_conformed'])})"
        )

    titles = report["titles"]
    console.print("\n[bold]2. Reported titles vs planned measures[/bold]")
    if not titles.get("computed"):
        console.print("  [yellow]run `endpoints results conform` to measure this[/yellow]")
    else:
        for method, count in titles["link_methods"].items():
            console.print(
                f"  {method:<24} {count:>7}  ({_pct(count, titles['reported_outcomes'])})"
            )
        console.print(
            f"  {'measurement_unmatched':<24} {titles['measurement_unmatched']:>7}  "
            f"({_pct(titles['measurement_unmatched'], titles['reported_outcomes'])})"
        )

    enums = report["enumerations"]
    console.print("\n[bold]3. param_type and dispersion_type[/bold]")
    if not enums.get("computed"):
        console.print("  [yellow]run `endpoints results conform` to measure this[/yellow]")
    else:
        for field in ("param_type_raw", "dispersion_type_raw"):
            console.print(f"  [dim]{field} -- {enums[field + '_distinct']} distinct value(s)[/dim]")
            table = Table("value", "folds to", "rows", box=None, pad_edge=False)
            for entry in enums[field]:
                table.add_row(entry["value"], entry["kind"] or "-", f"{entry['rows']:,}")
            console.print(table)
            unrecognised = enums[field + "_unrecognised"]
            if unrecognised:
                console.print(
                    "  [yellow]not recognised: "
                    + ", ".join(f"{e['value']!r} ({e['rows']:,})" for e in unrecognised)
                    + "[/yellow]"
                )

    units = report["units"]
    console.print("\n[bold]4. unit_of_measure against scales.yaml[/bold]")
    if not units.get("computed"):
        console.print("  [yellow]run `endpoints results conform` to measure this[/yellow]")
    else:
        console.print(
            f"  {units['resolved']} of {units['rows']} arm-level rows resolved to a scale "
            f"({_pct(units['resolved'], units['rows'])}); "
            f"{units['convertible_to_si']} carry a conversion factor "
            f"({_pct(units['convertible_to_si'], units['rows'])})"
        )
        if units["unresolved"]:
            console.print(
                f"  [yellow]{units['unresolved_distinct']} unresolved unit string(s): "
                + ", ".join(f"{e['value']!r} ({e['rows']:,})" for e in units["unresolved"])
                + "[/yellow]"
            )


@app.command()
def stats(
    measurement: Optional[str] = typer.Option(
        None, "--measurement", help="Vocabulary measurement id, e.g. fev1."
    ),
    form: Optional[str] = typer.Option(
        None, "--form", help="Vocabulary form id, e.g. change_from_baseline."
    ),
    scale: Optional[str] = typer.Option(
        None, "--scale", help="Pool only this unit, e.g. litres (the converted unit where "
        "scales.yaml declares a conversion)."
    ),
    timepoint: Optional[str] = typer.Option(
        None, "--timepoint", help="Timepoint pattern id, e.g. fixed_visit."
    ),
    ta: Optional[str] = typer.Option(None, "--ta", help="Primary therapeutic area id."),
    phase: Optional[str] = typer.Option(None, "--phase", help="Study phase, e.g. PHASE3."),
    source: str = typer.Option(
        "outcome",
        "--source",
        help='"outcome" (reported outcome measures, the default) or "baseline" (baseline '
        "characteristics -- a larger denominator, and a different quantity: see "
        "docs/ENDPOINT_RESULTS_SPEC.md, D8).",
    ),
    analyses: bool = typer.Option(
        False,
        "--analyses",
        help="Report effect sizes, p-values and non-inferiority margins instead of the SD "
        "distribution.",
    ),
    only_reported: bool = typer.Option(
        False, "--only-reported", help="Exclude every derived SD, leaving only reported ones."
    ),
    no_approximate: bool = typer.Option(
        False,
        "--no-approximate",
        help="Exclude the Wan et al. IQR/range estimates, which are approximations rather "
        "than conversions.",
    ),
    drug_class: Optional[str] = typer.Option(
        None,
        "--drug-class",
        help="Only studies whose interventions resolved to this drug class. The STUDY tier: "
        "it narrows to trials that used the class, and does not claim the SD came from an "
        "arm that received it.",
    ),
    by: Optional[str] = typer.Option(
        None,
        "--by",
        help="Stratify instead of filtering. Only 'drug-class' is supported. On the SD side "
        "this is a homogeneity check; on --analyses it is the point, because pooling effect "
        "sizes across mechanisms has no referent.",
    ),
    by_kind: str = typer.Option(
        "mechanism",
        "--by-kind",
        help="Which drug-class kind --by drug-class stratifies over: mechanism (default), "
        "pharmacologic, modality or control.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """The empirical distribution of arm-level variability for an endpoint.

    Grouped by form and unit, because the SD of a change from baseline is not
    the SD of a raw value and the SD in litres is not the SD in millilitres,
    and reported with the coverage line that says how many of the conformed
    studies actually contributed. Requires `endpoints results conform`.
    """
    if by is not None and by not in STATS_STRATIFIERS:
        console.print(f"[red]--by must be one of {STATS_STRATIFIERS}, got {by!r}[/red]")
        raise typer.Exit(code=1)
    if by == "drug-class" and drug_class:
        # The stratifier sets `drug_class` per stratum, so accepting both would
        # silently ignore the one the caller typed.
        console.print(
            "[red]--drug-class and --by drug-class are mutually exclusive: one filters to a "
            "single class, the other reports every class side by side. Pick one.[/red]"
        )
        raise typer.Exit(code=1)

    filters = StatsFilters(
        measurement=measurement, form=form, scale=scale, timepoint=timepoint, ta=ta,
        phase=phase, drug_class=drug_class, source=source,
        include_approximate=not no_approximate, include_derived=not only_reported,
    )
    con = connect(warehouse)
    try:
        try:
            if by == "drug-class":
                strata = stratify_by_drug_class(con, filters, kind=by_kind, analyses=analyses)
                report = None
            else:
                report = (
                    analysis_distribution(con, filters) if analyses else sd_distribution(con, filters)
                )
        except (NotComputed, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        con.close()

    if report is None:
        if as_json:
            print(
                json.dumps(
                    {
                        "stratified_by": "drug_class",
                        "kind": by_kind,
                        "strata": [
                            {"drug_class_id": class_id, **_stats_json(r, analyses=analyses)}
                            for class_id, r in strata
                        ],
                    },
                    indent=2,
                )
            )
            return
        if not strata:
            console.print(
                f"[yellow]No {by_kind} drug class covers any study in this selection -- "
                "nothing to stratify. `endpoints drug-class coverage` reports why.[/yellow]"
            )
            return
        for class_id, stratum in strata:
            console.print(f"\n[bold cyan]── {class_id} ──[/bold cyan]")
            if analyses:
                _print_analyses(stratum)
            else:
                _print_sd(stratum)
        console.print(
            f"\n[dim]{len(strata)} {by_kind} class(es), most-studied first. Strata are not "
            "disjoint: a combination trial appears under every class it used.[/dim]"
        )
        return

    if as_json:
        print(json.dumps(_stats_json(report, analyses=analyses), indent=2))
        return
    if analyses:
        _print_analyses(report)
    else:
        _print_sd(report)


def _describe_filters(filters: StatsFilters) -> str:
    parts = [f"{key}={value}" for key, value in (
        ("measurement", filters.measurement), ("form", filters.form), ("scale", filters.scale),
        ("timepoint", filters.timepoint), ("ta", filters.ta), ("phase", filters.phase),
        ("drug_class", filters.drug_class),
    ) if value]
    parts.append(f"source={filters.source}")
    return ", ".join(parts)


def _fmt(value, digits: int = 4) -> str:
    return "-" if value is None else f"{value:,.{digits}g}"


def _print_sd(report) -> None:
    filters = report["filters"]
    console.print(f"[bold]{_describe_filters(filters)}[/bold]")
    if not report["groups"]:
        console.print(
            "[yellow]No arm-level measurement yielded a usable dispersion for this "
            f"selection ({report['studies_conformed']} conformed study/studies matched).[/yellow]"
        )
        _print_skips(report["skip_reasons"])
        return

    for group in report["groups"]:
        header = f"{group.form_id or '(no form)'} · {group.scale_id or '(no unit)'}"
        if group.converted:
            header += "  [dim](converted via scales.yaml)[/dim]"
        if group.sd_scale != "arithmetic":
            header += f"  [yellow](SD on the {group.sd_scale} scale)[/yellow]"
        console.print(f"\n  [bold]{header}[/bold]")
        participants = f"{group.participants:,}" if group.participants is not None else "-"
        console.print(
            f"    studies {group.studies:<6} arms {group.arms:<6} participants {participants}"
        )
        console.print(
            f"    SD      median {_fmt(group.median)}   "
            f"IQR {_fmt(group.q1)}-{_fmt(group.q3)}   "
            f"range {_fmt(group.minimum)}-{_fmt(group.maximum)}"
        )
        console.print(
            "            " + " · ".join(f"{method} {count}" for method, count in group.methods.items())
        )
        if group.timepoints:
            console.print(
                "    timepoints  "
                + "  ".join(
                    f"{pattern or '(unresolved)'} ({count})" for pattern, count in group.timepoints[:6]
                )
            )
        console.print(
            f"    coverage    {group.studies} of {group.studies_conformed} conformed studies "
            f"reported a usable dispersion ({_pct(group.studies, group.studies_conformed)})"
        )

    _print_skips(report["skip_reasons"])


def _print_skips(skips: dict) -> None:
    if not skips:
        return
    console.print(
        "\n[dim]No SD from: "
        + " · ".join(f"{reason} {count}" for reason, count in skips.items())
        + "[/dim]"
    )


def _print_analyses(report) -> None:
    filters = report["filters"]
    console.print(f"[bold]{_describe_filters(filters)} -- reported analyses[/bold]")
    if not report["analyses"]:
        console.print("[yellow]No analysis was reported for this selection.[/yellow]")
        return

    if report["effects"]:
        console.print("\n  [bold]effect measures[/bold]")
        table = Table("effect", "unit", "studies", "analyses", "median", "IQR", "null",
                      box=None, pad_edge=False)
        for effect in report["effects"]:
            table.add_row(
                effect["effect_kind"],
                effect["scale_id"] or "-",
                str(effect["studies"]),
                str(effect["analyses"]),
                _fmt(effect["median"]),
                f"{_fmt(effect['q1'])}-{_fmt(effect['q3'])}",
                _fmt(effect["null_value"], 2),
            )
        console.print(table)

    p_values = report["p_values"]
    console.print("\n  [bold]p-values[/bold]")
    console.print(
        f"    stated {p_values['stated']} · exact {p_values['exact']} · "
        f"censored {p_values['censored']} · below 0.05 {p_values['below_0_05']} "
        f"({_pct(p_values['below_0_05'], p_values['stated'])} of stated)"
    )
    if p_values["censored"]:
        console.print(
            "    [dim]a censored p-value ('<0.001') contributes its bound, not an observed "
            "value[/dim]"
        )

    console.print("\n  [bold]non-inferiority[/bold]")
    if not report["non_inferiority"]:
        console.print("    none of these analyses was a non-inferiority comparison")
    else:
        table = Table("study", "effect", "margin", "from", box=None, pad_edge=False)
        for entry in report["non_inferiority"]:
            margin = "-" if entry["margin"] is None else (
                f"{entry['margin']:g}" + (f" {entry['margin_unit']}" if entry["margin_unit"] else "")
            )
            table.add_row(
                entry["nct_id"], entry["param_type"] or "-", margin,
                (entry["description"] or "(not stated)")[:60],
            )
        console.print(table)
        unparsed = sum(1 for e in report["non_inferiority"] if e["margin"] is None)
        if unparsed:
            console.print(
                f"    [dim]{unparsed} margin(s) could not be read out of the description; the "
                "description is shown instead of a guessed number[/dim]"
            )

    console.print(
        f"\n  coverage    {report['studies_with_analyses']} of {report['studies_conformed']} "
        f"conformed studies reported at least one analysis "
        f"({_pct(report['studies_with_analyses'], report['studies_conformed'])})"
    )


def _stats_json(report, *, analyses: bool) -> dict:
    filters = report["filters"]
    body = {
        "filters": {k: v for k, v in filters.__dict__.items()},
        "studies_conformed": report["studies_conformed"],
    }
    if analyses:
        return {
            **body,
            "analyses": report["analyses"],
            "studies_with_analyses": report["studies_with_analyses"],
            "effects": report["effects"],
            "p_values": report["p_values"],
            "non_inferiority": report["non_inferiority"],
        }
    return {
        **body,
        "studies_with_sd": report["studies_with_sd"],
        "skip_reasons": report["skip_reasons"],
        "groups": [
            {
                **group.__dict__,
                "coverage": group.coverage,
            }
            for group in report["groups"]
        ],
    }


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    warehouse: str = typer.Option("warehouse.duckdb", "--warehouse"),
) -> None:
    """Serve the read-only USDM 4.0 endpoints API.

    `GET /v4/studies/{nctId}/endpoints` is the one call this exists for.
    Requires the `serve` extra (`uv sync --extra serve`).
    """
    try:
        import uvicorn

        from clinical_endpoints.usdm.api import create_app
    except ImportError as exc:
        console.print(
            "[red]The API needs FastAPI and uvicorn, which are an optional extra:[/red]\n"
            "  uv sync --extra serve"
        )
        raise typer.Exit(code=1) from exc

    uvicorn.run(create_app(warehouse), host=host, port=port)


def _split_option(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    return [token.strip() for token in value.split(",") if token.strip()]


@review_app.command("list")
def review_list(
    status: str = typer.Option("pending", "--status"),
    reason: Optional[str] = typer.Option(None, "--reason"),
    limit: int = typer.Option(20, "--limit"),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """List conformed.review_queue entries -- run `endpoints conform` first."""
    con = connect(warehouse)
    try:
        if not _table_exists(con, "conformed", "review_queue"):
            console.print("[red]No conformed.review_queue yet -- run `endpoints conform` first.[/red]")
            raise typer.Exit(code=1)

        where = ["status = ?"]
        params: list = [status]
        if reason:
            where.append("reason = ?")
            params.append(reason)
        params.append(limit)

        rows = con.execute(
            f"""
            SELECT review_id, nct_id, reason, measure_raw, best_semantic_candidate, best_semantic_score
            FROM conformed.review_queue
            WHERE {' AND '.join(where)}
            ORDER BY queued_at
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        con.close()

    table = Table("review_id", "nct_id", "reason", "measure", "best candidate", "score")
    for review_id, nct_id, row_reason, measure_raw, candidate, score in rows:
        table.add_row(
            review_id[:12], nct_id, row_reason, (measure_raw or "")[:60],
            candidate or "-", f"{score:.2f}" if score is not None else "-",
        )
    console.print(table)
    console.print(f"Showing {len(rows)} row(s) with status={status!r}" + (f", reason={reason!r}" if reason else ""))


@review_app.command("resolve")
def review_resolve(
    review_id: str = typer.Argument(...),
    vocab_term_id: Optional[str] = typer.Argument(None),
    new_term: bool = typer.Option(False, "--new-term"),
) -> None:
    """Resolve a review_queue entry against an existing or new vocab term."""
    _not_yet_implemented(
        "review resolve",
        "edit vocab/*.yaml, then re-run `vocab validate` and `conform`",
    )


@app.command()
def query(sql: str = typer.Argument(...)) -> None:
    """Run arbitrary SQL against the warehouse and print the result."""
    _not_yet_implemented("query", 'use `duckdb warehouse.duckdb -c "<sql>"`')


@app.command()
def export(
    query_str: str = typer.Option(..., "--query"),
    fmt: str = typer.Option("csv", "--format"),
    out: str = typer.Option(..., "--out"),
) -> None:
    """Run SQL and export the result as parquet/csv/json."""
    _not_yet_implemented(
        "export", "use `duckdb warehouse.duckdb -c \"COPY (<sql>) TO 'out.parquet'\"`"
    )


if __name__ == "__main__":
    app()
