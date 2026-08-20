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

SOURCES = ("aact", "ctgov_api")

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

vocab_app = typer.Typer(no_args_is_help=True, help="Vocabulary sampling / validation.")
review_app = typer.Typer(no_args_is_help=True, help="Review-queue management.")
ta_app = typer.Typer(no_args_is_help=True, help="Therapeutic-area mapping.")
usdm_app = typer.Typer(no_args_is_help=True, help="CDISC USDM 4.0 projection.")
app.add_typer(vocab_app, name="vocab")
app.add_typer(review_app, name="review")
app.add_typer(ta_app, name="ta")
app.add_typer(usdm_app, name="usdm")

USDM_ENVELOPES = ("module", "wrapper")


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


def _vocab_ta_tables_ready(con) -> bool:
    """Whether `vocab validate` has already loaded the TA mapping into this
    warehouse -- the TA resolver reads vocab.ta_mesh_* tables, not the YAML
    directly (except for the two `defaults`, which aren't persisted at all)."""
    return _table_exists(con, "vocab", "ta_mesh_term_overrides") and _table_exists(
        con, "vocab", "therapeutic_areas"
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
        "e.g. oncology,respiratory). Requires `endpoints vocab validate` to have already "
        "been run against this warehouse.",
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
    """Pull filtered studies + design_outcomes/conditions into raw.*, log the pull,
    and (once `vocab validate` has loaded the TA mapping) resolve therapeutic areas
    for the pulled studies into conformed.study_therapeutic_area -- filtering down
    to `--ta` if given."""
    if source not in SOURCES:
        console.print(f"[red]--source must be one of {SOURCES}, got {source!r}[/red]")
        raise typer.Exit(code=1)

    ta_ids: Optional[tuple] = None
    if ta:
        ta_ids = tuple(t.strip() for t in ta.split(",") if t.strip())

    since_date: Optional[dt.date] = None
    if since:
        try:
            since_date = dt.date.fromisoformat(since)
        except ValueError as exc:
            console.print(f"[red]--since must be YYYY-MM-DD, got {since!r}[/red]")
            raise typer.Exit(code=1) from exc

    phases = tuple(p.strip() for p in phase.split(",") if p.strip())
    filters = PullFilters(phases=phases, limit=limit, since=since_date, ta=ta_ids)

    con = connect(warehouse)
    try:
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
    except SchemaMigrationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        con.close()

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
            "[yellow]Skipped therapeutic-area resolution: run `endpoints vocab validate` "
            "to populate conformed.study_therapeutic_area.[/yellow]"
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
