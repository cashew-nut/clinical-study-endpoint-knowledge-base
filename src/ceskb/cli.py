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
    ceskb evaluate                  score classifications against a gold set
    ceskb agreement A B             inter-annotator agreement between two gold sets
    ceskb review                    list specifications awaiting human review
    ceskb override                  record a reviewer decision
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
    from ceskb.evaluate.gold import GoldError, check_gold_set, load_gold_sets
    from ceskb.review.overrides import OverrideError, check_overrides, load_overrides
    from ceskb.vocab.loader import VocabularyError, reload_vocabulary

    try:
        vocab = reload_vocabulary()
    except VocabularyError as exc:
        print(f"vocabulary invalid:\n{exc}", file=sys.stderr)
        return 1

    # Overrides and gold sets name concepts and terms, so a typo in either is a broken
    # reference that should fail here rather than silently never applying.
    problems: list[str] = []
    try:
        overrides = load_overrides()
        problems += check_overrides(overrides, vocab)
    except OverrideError as exc:
        problems.append(str(exc))
        overrides = []
    try:
        gold_sets = load_gold_sets()
        for gold in gold_sets:
            problems += [f"{gold.gold_set_id}: {p}" for p in check_gold_set(gold, vocab)]
    except GoldError as exc:
        problems.append(str(exc))
        gold_sets = []

    if problems:
        print("review data invalid:\n" + "\n".join(f"  {p}" for p in problems), file=sys.stderr)
        return 1

    _print(
        {
            "ok": True,
            "axes": len(vocab.axes),
            "terms": sum(len(a.terms) for a in vocab.axes.values()),
            "concepts": len(vocab.concepts),
            "rules": len(vocab.rules),
            "overrides": len(overrides),
            "gold_sets": {g.gold_set_id: len(g.items) for g in gold_sets},
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
    from ceskb.review.overrides import load_overrides_into_db
    from ceskb.store.db import connect, initialise, load_vocabulary_into_db

    with connect(args.database) as conn:
        initialise(conn)
        stats = load_vocabulary_into_db(conn)
        stats["overrides"] = load_overrides_into_db(conn)["total"]
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
    from ceskb.review.overrides import load_overrides_into_db
    from ceskb.store.db import connect, initialise, load_vocabulary_into_db

    args.incremental = True
    source = _build_source(args)
    try:
        with connect(args.database) as conn:
            initialise(conn)
            vocab_stats = load_vocabulary_into_db(conn)
            ingest_stats = ingest(conn, source)
            # After ingest so staleness is judged against the text just fetched, and
            # before classify so reviewer decisions are in force during derivation.
            override_stats = load_overrides_into_db(conn)
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
            "overrides": override_stats,
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


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Score classifications against gold annotations, and gate on the result."""
    from ceskb.evaluate.gold import GoldError, load_gold_set, load_gold_sets
    from ceskb.evaluate.score import check_thresholds, persist, score
    from ceskb.store.db import connect

    try:
        gold_sets = [load_gold_set(args.gold)] if args.gold else load_gold_sets()
    except GoldError as exc:
        print(f"gold set invalid: {exc}", file=sys.stderr)
        return 1
    if not gold_sets:
        print(
            f"no gold sets found in {PATHS.gold}.\n"
            "Without annotations only coverage can be reported, never accuracy.",
            file=sys.stderr,
        )
        return 1

    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    with connect(args.database) as conn:
        for gold in gold_sets:
            report = score(conn, gold)
            report["evaluation_id"] = persist(conn, report)
            reports.append(report)
            failures += [
                f"{gold.gold_set_id}: {failure}"
                for failure in check_thresholds(
                    report,
                    min_accuracy=args.min_accuracy,
                    min_precision=args.min_precision,
                    min_recall=args.min_recall,
                    max_excluded=args.max_excluded,
                )
            ]

    if args.json:
        _print(reports if len(reports) > 1 else reports[0])
    else:
        for report in reports:
            _print_evaluation(report)
    if failures:
        print("\ngate failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


def _print_evaluation(report: dict[str, Any]) -> None:
    print(f"\ngold set   {report['gold_set_id']}  (annotator: {report['annotator']})")
    print(f"scored     {report['items_scored']} of {report['items_total']} items", end="")
    if report["items_excluded"]:
        reasons = ", ".join(
            sorted({e["why"] for e in report["excluded"]})
        )
        print(f"   [{report['items_excluded']} excluded: {reasons}]")
    else:
        print()
    print(f"accuracy   {report['concept_accuracy']:.1%}   "
          f"macro P {report['macro_precision']:.3f}  "
          f"R {report['macro_recall']:.3f}  "
          f"F1 {report['macro_f1']:.3f}")

    if report["by_difficulty"]:
        band = "  ".join(
            f"{level} {m['accuracy']:.0%} ({m['items']})"
            for level, m in report["by_difficulty"].items()
        )
        print(f"by call    {band}")

    if report["axis_accuracy"]:
        print("\naxis accuracy (only axes the annotator judged)")
        for axis_id, metrics in report["axis_accuracy"].items():
            print(f"  {axis_id:<24} {metrics['accuracy']:>6.1%}  "
                  f"{metrics['correct']}/{metrics['judged']}")

    if report["errors"]:
        print(f"\n{len(report['errors'])} disagreement(s)")
        for error in report["errors"]:
            print(f"  {error['outcome_uid']}")
            print(f"    gold      {error['gold']}")
            print(f"    predicted {error['predicted']}")
            if error["text"]:
                print(f"    text      {error['text'][:96]}")

    if report["axis_errors"]:
        print(f"\n{len(report['axis_errors'])} axis disagreement(s)")
        for error in report["axis_errors"][:20]:
            print(f"  {error['outcome_uid']}  {error['axis_id']}: "
                  f"expected {error['expected']}, got {error['actual']}")

    if "caveat" in report:
        print(f"\n! {report['caveat']}")


def cmd_agreement(args: argparse.Namespace) -> int:
    """Compare two independent annotations of the same outcomes."""
    from ceskb.evaluate.gold import GoldError, agreement, load_gold_set

    try:
        first, second = load_gold_set(args.first), load_gold_set(args.second)
    except GoldError as exc:
        print(f"gold set invalid: {exc}", file=sys.stderr)
        return 1

    result = agreement(first, second)
    _print(result)
    if result["items_shared"] == 0:
        print("the two sets share no outcomes, so there is nothing to compare",
              file=sys.stderr)
        return 1
    if args.min_kappa is not None:
        kappa = result["cohens_kappa"]
        if kappa is None or kappa < args.min_kappa:
            print(
                f"\nagreement {kappa} below required {args.min_kappa}: the annotation "
                "task is underspecified, so fix the guideline or the vocabulary before "
                "tuning the classifier",
                file=sys.stderr,
            )
            return 1
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """List specifications a human should look at, most doubtful first."""
    from ceskb.store.db import connect

    sql = """
        SELECT spec_id, outcome_uid, concept_id, reason, review_score,
               match_confidence, competing_rule_count, unresolved_count, measure
        FROM review_queue
    """
    params: list[Any] = []
    if args.reason:
        sql += " WHERE reason = ?"
        params.append(args.reason)
    sql += " ORDER BY review_score DESC, outcome_uid LIMIT ?"
    params.append(args.limit)

    with connect(args.database, read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()
        by_reason = conn.execute(
            "SELECT reason, count(*) FROM review_queue GROUP BY reason ORDER BY 2 DESC"
        ).fetchall()
        decided = conn.execute(
            "SELECT status, count(*) FROM spec_override GROUP BY status"
        ).fetchall()

    _print(
        {
            "queue_depth_by_reason": dict(by_reason),
            "decisions_recorded": dict(decided),
            "queue": [
                dict(
                    zip(
                        [
                            "spec_id", "outcome_uid", "concept_id", "reason", "review_score",
                            "match_confidence", "competing_rules", "unresolved_axes", "measure",
                        ],
                        row,
                    )
                )
                for row in rows
            ],
        }
    )
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    """Record a reviewer decision about one outcome."""
    from ceskb.classify.engine import classify_all
    from ceskb.review.overrides import KEEP_CONCEPT, OverrideError, record_override
    from ceskb.store.db import connect

    if args.concept is not None and args.no_concept:
        print("--concept and --no-concept contradict each other", file=sys.stderr)
        return 1
    if args.concept is None and not args.no_concept and not args.axis:
        print(
            "nothing to record: give --concept CONCEPT_ID, --no-concept "
            "(nothing in the vocabulary fits), or at least one --axis AXIS=TERM",
            file=sys.stderr,
        )
        return 1

    axes: dict[str, str] = {}
    for pair in args.axis or []:
        if "=" not in pair:
            print(f"--axis expects axis_id=term_id, got '{pair}'", file=sys.stderr)
            return 1
        axis_id, term_id = pair.split("=", 1)
        axes[axis_id.strip()] = term_id.strip()

    with connect(args.database) as conn:
        try:
            override = record_override(
                conn,
                outcome_uid=args.outcome_uid,
                reason=args.reason,
                reviewer=args.reviewer,
                concept_id=(
                    None
                    if args.no_concept
                    else (args.concept if args.concept is not None else KEEP_CONCEPT)
                ),
                axes=axes,
                notes=args.notes,
            )
        except OverrideError as exc:
            print(f"override rejected: {exc}", file=sys.stderr)
            return 1
        # Re-derive so the decision is reflected immediately rather than at the next
        # scheduled refresh.
        stats = classify_all(conn)

    _print({"recorded": override.to_dict(), "written_to": str(PATHS.overrides),
            "reclassified": stats})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    from ceskb.api.app import ALLOW_REVIEW_ENV

    # Passed through the environment because uvicorn loads the app by import string and
    # never sees these arguments.
    if args.allow_review:
        os.environ[ALLOW_REVIEW_ENV] = "1"
        print(
            f"review writes ENABLED: the UI may record decisions to {PATHS.overrides} "
            "and re-derive Layer B",
            file=sys.stderr,
        )
    else:
        os.environ.pop(ALLOW_REVIEW_ENV, None)

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

    eval_p = sub.add_parser("evaluate", help="score classifications against a gold set")
    eval_p.add_argument("--gold", help="a single gold set file; default is every file in review/gold")
    eval_p.add_argument("--min-accuracy", type=float, default=None,
                        help="fail below this overall concept accuracy")
    eval_p.add_argument("--min-precision", type=float, default=None,
                        help="fail if any attested concept falls below this precision")
    eval_p.add_argument("--min-recall", type=float, default=None,
                        help="fail below this macro recall")
    eval_p.add_argument("--max-excluded", type=int, default=None,
                        help="fail if more than this many gold items could not be scored")
    eval_p.add_argument("--json", action="store_true", help="emit the full report as JSON")
    eval_p.set_defaults(func=cmd_evaluate)

    agree_p = sub.add_parser("agreement", help="inter-annotator agreement between two gold sets")
    agree_p.add_argument("first")
    agree_p.add_argument("second")
    agree_p.add_argument("--min-kappa", type=float, default=None,
                         help="fail below this chance-corrected agreement")
    agree_p.set_defaults(func=cmd_agreement)

    review_p = sub.add_parser("review", help="list specifications awaiting human review")
    review_p.add_argument("--limit", type=int, default=25)
    review_p.add_argument(
        "--reason",
        choices=["arbitrary_tie_break", "contested_match", "low_confidence",
                 "unresolved_parameters"],
        help="show only one kind of doubt",
    )
    review_p.set_defaults(func=cmd_review)

    override_p = sub.add_parser("override", help="record a reviewer decision")
    override_p.add_argument("outcome_uid")
    override_p.add_argument("--concept", help="the concept this outcome actually is")
    override_p.add_argument("--no-concept", action="store_true",
                            help="record that no concept in the vocabulary fits")
    override_p.add_argument("--axis", action="append", metavar="AXIS=TERM",
                            help="correct one axis; repeat for several")
    override_p.add_argument("--reason", required=True, help="why (stored, and required)")
    override_p.add_argument("--reviewer", required=True)
    override_p.add_argument("--notes")
    override_p.set_defaults(func=cmd_override)

    serve_p = sub.add_parser("serve", help="run the exploration UI")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--reload", action="store_true")
    serve_p.add_argument(
        "--allow-review",
        action="store_true",
        help="permit the UI to record reviewer decisions; off by default, since it "
             "writes review/overrides.yaml and re-derives Layer B",
    )
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
