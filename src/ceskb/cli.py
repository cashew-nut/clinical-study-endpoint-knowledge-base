"""Command line interface.

    ceskb init                      create the database and load the vocabulary
    ceskb validate                  validate vocabularies and rule packs, no database
    ceskb ingest --source ctgov     pull studies from ClinicalTrials.gov
    ceskb ingest --source fixtures  load the synthetic corpus
    ceskb probe --source ctgov      check the source schema against declared field paths
    ceskb classify                  derive Layer B specifications
    ceskb project                   derive Layer C USDM documents
    ceskb refresh                   incremental ingest + classify + project
    ceskb export                    write JSON/Parquet/USDM artefacts
    ceskb stats                     coverage and prevalence summary
    ceskb serve                     run the exploration UI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ceskb.config import DERIVATION_VERSION, PATHS


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


#: Named scopes, so a routine slice does not have to be retyped or half-remembered.
#: A preset supplies defaults only; any explicit flag on the command line wins.
PRESETS: dict[str, dict[str, Any]] = {
    "phase3-recent-100": {
        "source": "ctgov",
        "phase": ["PHASE3"],
        "study_type": "INTERVENTIONAL",
        "sort": "LastUpdatePostDate:desc",
        "max_studies": 100,
        "_note": "The 100 most recently updated phase 3 interventional studies.",
    },
    "phase3-recent-1000": {
        "source": "ctgov",
        "phase": ["PHASE3"],
        "study_type": "INTERVENTIONAL",
        "sort": "LastUpdatePostDate:desc",
        "max_studies": 1000,
        "_note": "As above, at a size where coverage numbers start to mean something.",
    },
    "phase2-3-oncology": {
        "source": "ctgov",
        "phase": ["PHASE2", "PHASE3"],
        "study_type": "INTERVENTIONAL",
        "condition": "cancer",
        "sort": "LastUpdatePostDate:desc",
        "max_studies": 500,
        "_note": "Oncology slice, the densest area of the concept set.",
    },
}


def _apply_preset(args: argparse.Namespace, argv: list[str]) -> None:
    """Fill unset options from a preset, leaving anything explicit untouched."""
    name = getattr(args, "preset", None)
    if not name:
        return
    supplied = {token.split("=", 1)[0] for token in argv if token.startswith("--")}
    for key, value in PRESETS[name].items():
        if key.startswith("_"):
            continue
        if f"--{key.replace('_', '-')}" in supplied:
            continue
        setattr(args, key, value)


def cmd_validate(args: argparse.Namespace) -> int:
    from ceskb.vocab.loader import VocabularyError, reload_vocabulary

    try:
        vocab = reload_vocabulary()
    except VocabularyError as exc:
        print(f"vocabulary invalid:\n{exc}", file=sys.stderr)
        return 1
    _print(
        {
            "ok": True,
            "axes": len(vocab.axes),
            "terms": sum(len(a.terms) for a in vocab.axes.values()),
            "concepts": len(vocab.concepts),
            "rules": len(vocab.rules),
            "unverified_external_mappings": sum(
                1
                for term in vocab.terms()
                for mapping in term.external_mappings
                if not mapping.get("verified")
            ),
        }
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from ceskb.store.db import connect, initialise, load_vocabulary_into_db

    with connect(args.database) as conn:
        initialise(conn)
        stats = load_vocabulary_into_db(conn)
    _print({"database": str(args.database or PATHS.database), "loaded": stats})
    return 0


def _build_source(args: argparse.Namespace):
    from ceskb.ingest.sources import CtgovApiSource, FixtureSource
    from ceskb.store.db import connect, get_watermark

    if args.source == "fixtures":
        return FixtureSource(directory=Path(args.fixtures) if args.fixtures else None)

    updated_since = args.updated_since
    if updated_since is None and getattr(args, "incremental", False):
        with connect(args.database, read_only=True) as conn:
            updated_since = get_watermark(conn, "clinicaltrials.gov", "last_update_posted")
    phases = tuple(p.strip().upper() for p in (args.phase or []) if p.strip())
    return CtgovApiSource(
        query_term=args.query,
        condition=args.condition,
        updated_since=updated_since,
        max_studies=args.max_studies,
        phases=phases,
        study_type=args.study_type,
        sort=args.sort,
    )


def cmd_ingest(args: argparse.Namespace) -> int:
    from ceskb.ingest.pipeline import ingest
    from ceskb.ingest.sources import SourceError
    from ceskb.store.db import connect, initialise

    source = _build_source(args)
    try:
        with connect(args.database) as conn:
            initialise(conn)
            stats = ingest(conn, source)
    except SourceError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 2
    _print({"source": source.describe(), "stats": stats.as_dict()})
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from ceskb.ingest.pipeline import probe
    from ceskb.ingest.sources import SourceError

    source = _build_source(args)
    try:
        result = probe(source, limit=args.limit)
    except SourceError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 2
    missing = [name for name, info in result.items() if info["present"] == 0]
    _print({"source": source.describe(), "fields": result, "absent": missing})
    return 1 if missing else 0


def cmd_classify(args: argparse.Namespace) -> int:
    from ceskb.classify.engine import classify_all
    from ceskb.store.db import connect, initialise

    with connect(args.database) as conn:
        initialise(conn)
        stats = classify_all(conn, limit=args.limit)
    _print(stats)
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    from ceskb.project.usdm import project_all
    from ceskb.store.db import connect, initialise

    with connect(args.database) as conn:
        initialise(conn)
        stats = project_all(conn)
    _print(stats)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Incremental end-to-end update, the operation a scheduler should call."""
    from ceskb.classify.engine import classify_all
    from ceskb.ingest.pipeline import ingest
    from ceskb.ingest.sources import SourceError
    from ceskb.project.usdm import project_all
    from ceskb.store.db import connect, initialise, load_vocabulary_into_db

    args.incremental = True
    source = _build_source(args)
    try:
        with connect(args.database) as conn:
            initialise(conn)
            vocab_stats = load_vocabulary_into_db(conn)
            ingest_stats = ingest(conn, source)
            classify_stats = classify_all(conn)
            project_stats = project_all(conn)
    except SourceError as exc:
        print(f"refresh failed at ingest: {exc}", file=sys.stderr)
        return 2
    _print(
        {
            "source": source.describe(),
            "vocabulary": vocab_stats,
            "ingest": ingest_stats.as_dict(),
            "classify": classify_stats,
            "project": project_stats,
            "derivation_version": DERIVATION_VERSION,
        }
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from ceskb.store.export import export_all
    from ceskb.store.db import connect

    with connect(args.database, read_only=True) as conn:
        written = export_all(conn, Path(args.out) if args.out else None)
    _print({"written": [str(p) for p in written]})
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from ceskb.store.db import connect

    with connect(args.database, read_only=True) as conn:
        coverage = conn.execute("SELECT * FROM coverage_summary ORDER BY 1").fetchall()
        top = conn.execute(
            "SELECT concept_id, label, spec_count, study_count, primary_count "
            "FROM concept_prevalence ORDER BY spec_count DESC LIMIT 15"
        ).fetchall()
        totals = conn.execute(
            "SELECT (SELECT count(*) FROM study), (SELECT count(*) FROM study_outcome), "
            "(SELECT count(*) FROM endpoint_spec), (SELECT count(*) FROM usdm_projection)"
        ).fetchone()
    _print(
        {
            "studies": totals[0],
            "outcomes": totals[1],
            "endpoint_specs": totals[2],
            "usdm_projections": totals[3],
            "coverage_by_level": [
                dict(zip(["level", "total", "classified", "unclassified", "pct"], row))
                for row in coverage
            ],
            "most_common_concepts": [
                dict(zip(["concept_id", "label", "specs", "studies", "primary"], row))
                for row in top
            ],
        }
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("ceskb.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ceskb", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", help="path to the DuckDB file", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="validate vocabularies and rule packs").set_defaults(
        func=cmd_validate
    )
    sub.add_parser("init", help="create the database and load the vocabulary").set_defaults(
        func=cmd_init
    )

    def add_source_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--source", choices=["ctgov", "fixtures"], default="fixtures")
        p.add_argument("--query", help="free-text query (ctgov)")
        p.add_argument("--condition", help="condition query (ctgov)")
        p.add_argument("--updated-since", help="ISO date; only studies updated on or after")
        p.add_argument("--max-studies", type=int, default=None)
        p.add_argument(
            "--phase",
            action="append",
            choices=["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"],
            help="restrict to a study phase; repeat to allow several",
        )
        p.add_argument(
            "--study-type",
            choices=["INTERVENTIONAL", "OBSERVATIONAL", "EXPANDED_ACCESS"],
            default=None,
        )
        p.add_argument(
            "--sort",
            default=None,
            help="server-side sort, e.g. LastUpdatePostDate:desc for most-recent-first",
        )
        p.add_argument("--fixtures", help="fixture directory")
        p.add_argument(
            "--incremental",
            action="store_true",
            help="resume from the stored last-update watermark",
        )
        p.add_argument(
            "--preset",
            choices=sorted(PRESETS),
            help="apply a named scope preset before any explicit flags",
        )

    ingest_p = sub.add_parser("ingest", help="pull studies from a source")
    add_source_args(ingest_p)
    ingest_p.set_defaults(func=cmd_ingest)

    probe_p = sub.add_parser("probe", help="check a source against declared field paths")
    add_source_args(probe_p)
    probe_p.add_argument("--limit", type=int, default=50)
    probe_p.set_defaults(func=cmd_probe)

    classify_p = sub.add_parser("classify", help="derive Layer B specifications")
    classify_p.add_argument("--limit", type=int, default=None)
    classify_p.set_defaults(func=cmd_classify)

    sub.add_parser("project", help="derive Layer C USDM documents").set_defaults(func=cmd_project)

    refresh_p = sub.add_parser("refresh", help="incremental ingest, classify and project")
    add_source_args(refresh_p)
    refresh_p.set_defaults(func=cmd_refresh)

    export_p = sub.add_parser("export", help="write export artefacts")
    export_p.add_argument("--out", help="output directory")
    export_p.set_defaults(func=cmd_export)

    sub.add_parser("stats", help="coverage and prevalence summary").set_defaults(func=cmd_stats)

    serve_p = sub.add_parser("serve", help="run the exploration UI")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--reload", action="store_true")
    serve_p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_preset(args, argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
