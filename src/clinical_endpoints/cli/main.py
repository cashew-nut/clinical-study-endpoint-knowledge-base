"""`endpoints` CLI (plan §7). Only `pull` is implemented in build-order step 1;
the rest of the interface is stubbed so `--help` reflects the full intended
shape, and each stub names the step that implements it.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

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
graph_app = typer.Typer(no_args_is_help=True, help="Graph layer.")
ta_app = typer.Typer(no_args_is_help=True, help="Therapeutic-area mapping.")
app.add_typer(vocab_app, name="vocab")
app.add_typer(review_app, name="review")
app.add_typer(graph_app, name="graph")
app.add_typer(ta_app, name="ta")


def _not_yet_implemented(command: str, step: str) -> None:
    console.print(
        f"[yellow]`{command}` is not implemented yet -- it lands in build-order {step}.[/yellow]"
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
            result = aact_backend.run_pull(con, filters)
        else:
            try:
                result = ctgov_api_backend.run_pull(con, filters)
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
                result["row_counts"] = filter_raw_tables_by_nct_ids(con, keep)
                ta_summary = run_ta_resolution(con)  # re-derive the distribution for the kept studies only
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        con.close()

    console.print(
        f"[green]Pull {result['pull_id']} complete[/green] "
        f"(source={source}, {result['pulled_at'].isoformat()}): "
        f"{result['row_counts']['studies']} studies, "
        f"{result['row_counts']['design_outcomes']} design_outcomes -> raw.*"
    )
    if ta_ids:
        console.print(f"[green]Filtered to --ta {list(ta_ids)}[/green]")
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
    """Task 3: run the tree-prefix layer alone and the regex layer alone over every
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
def conform() -> None:
    """Normalize -> syntactic rules -> semantic fallback -> review queue."""
    _not_yet_implemented("conform", "step 3 (conforming pipeline)")


@review_app.command("list")
def review_list(status: str = typer.Option("pending", "--status")) -> None:
    """List review_queue entries."""
    _not_yet_implemented("review list", "step 3 (conforming pipeline)")


@review_app.command("resolve")
def review_resolve(
    review_id: str = typer.Argument(...),
    vocab_term_id: Optional[str] = typer.Argument(None),
    new_term: bool = typer.Option(False, "--new-term"),
) -> None:
    """Resolve a review_queue entry against an existing or new vocab term."""
    _not_yet_implemented("review resolve", "step 3 (conforming pipeline)")


@graph_app.command("build")
def graph_build() -> None:
    """Materialize graph.nodes / graph.edges, including SAME_MEASUREMENT_DIFFERENT_FORM."""
    _not_yet_implemented("graph build", "step 4 (graph layer + CLI polish)")


@app.command()
def query(sql: str = typer.Argument(...)) -> None:
    """Run arbitrary SQL against the warehouse and print the result."""
    _not_yet_implemented("query", "step 4 (graph layer + CLI polish)")


@app.command()
def export(
    query_str: str = typer.Option(..., "--query"),
    fmt: str = typer.Option("csv", "--format"),
    out: str = typer.Option(..., "--out"),
) -> None:
    """Run SQL and export the result as parquet/csv/json."""
    _not_yet_implemented("export", "step 4 (graph layer + CLI polish)")


if __name__ == "__main__":
    app()
