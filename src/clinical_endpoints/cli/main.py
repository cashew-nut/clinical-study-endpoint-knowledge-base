"""`endpoints` CLI (plan §7). Only `pull` is implemented in build-order step 1;
the rest of the interface is stubbed so `--help` reflects the full intended
shape, and each stub names the step that implements it.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import typer
from rich.console import Console

from clinical_endpoints.db import (
    AactConnectionError,
    MissingAactCredentialsError,
    attach_aact,
    connect,
)
from clinical_endpoints.ingest.pull import PullFilters, run_pull

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

vocab_app = typer.Typer(no_args_is_help=True, help="Vocabulary sampling / validation.")
review_app = typer.Typer(no_args_is_help=True, help="Review-queue management.")
graph_app = typer.Typer(no_args_is_help=True, help="Graph layer.")
app.add_typer(vocab_app, name="vocab")
app.add_typer(review_app, name="review")
app.add_typer(graph_app, name="graph")


def _not_yet_implemented(command: str, step: str) -> None:
    console.print(
        f"[yellow]`{command}` is not implemented yet -- it lands in build-order {step}.[/yellow]"
    )
    raise typer.Exit(code=1)


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
        help="Therapeutic-area filter (comma-separated). Requires the MeSH->TA vocab "
        "mapping built in build-order step 2, not yet available.",
    ),
    warehouse: str = typer.Option(
        "warehouse.duckdb", "--warehouse", help="Path to the DuckDB warehouse file."
    ),
) -> None:
    """Pull filtered studies + design_outcomes from AACT into raw.*, and log the pull."""
    if ta:
        console.print(
            "[red]--ta is not supported yet: therapeutic area is derived from the MeSH "
            "condition mapping built in build-order step 2 (vocab), not a native AACT "
            "field. Run without --ta for now.[/red]"
        )
        raise typer.Exit(code=1)

    since_date: Optional[dt.date] = None
    if since:
        try:
            since_date = dt.date.fromisoformat(since)
        except ValueError as exc:
            console.print(f"[red]--since must be YYYY-MM-DD, got {since!r}[/red]")
            raise typer.Exit(code=1) from exc

    phases = tuple(p.strip() for p in phase.split(",") if p.strip())
    filters = PullFilters(phases=phases, limit=limit, since=since_date)

    con = connect(warehouse)
    try:
        attach_aact(con)
    except (MissingAactCredentialsError, AactConnectionError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        result = run_pull(con, filters)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        con.close()

    console.print(
        f"[green]Pull {result['pull_id']} complete[/green] "
        f"({result['pulled_at'].isoformat()}): "
        f"{result['row_counts']['studies']} studies, "
        f"{result['row_counts']['design_outcomes']} design_outcomes -> raw.*"
    )


@vocab_app.command("sample")
def vocab_sample(
    limit: int = typer.Option(500, "--limit"),
    out: str = typer.Option("vocab_review.csv", "--out"),
) -> None:
    """Export distinct measure/description/time_frame strings with frequency counts."""
    _not_yet_implemented("vocab sample", "step 2 (checkpoint back to chat, not Claude Code)")


@vocab_app.command("validate")
def vocab_validate() -> None:
    """Load vocab/*.yaml into vocab.* tables; check uniqueness, no orphan synonyms."""
    _not_yet_implemented("vocab validate", "step 2 (checkpoint back to chat, not Claude Code)")


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
