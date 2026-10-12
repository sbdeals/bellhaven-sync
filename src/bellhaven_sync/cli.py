"""Command line: `bellhaven-sync run | scrape | status | review`.

`run` is read-only against the CRM (it only records proposals in the local store); the
only path that writes to the CRM is an Approve click in the review app.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .config import Settings, get_settings
from .models import PRIORITY_ORDER

log = logging.getLogger("bellhaven_sync.cli")

PRIORITIES = ("high", "medium", "low")


# --------------------------------------------------------------------------- seams


def _make_crm(settings: Settings) -> Any:
    from .crm import CRMClient

    return CRMClient(settings.base_url, settings.token)


def _scrape(settings: Settings) -> Any:
    from .scraper import http_fetcher, scrape_site

    fetcher = http_fetcher(settings.site_url)
    try:
        return scrape_site(fetcher, settings.site_url)
    finally:
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()


def _settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    if getattr(args, "db", None):
        settings = replace(settings, db_path=Path(args.db))
    return settings


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    cols = [list(map(str, headers))] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    for i, r in enumerate(cols):
        print("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))


# --------------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import run
    from .store import Store

    settings = _settings(args)
    if not settings.token:
        print("BELLHAVEN_CRM_TOKEN is not set (env or .env); cannot read the CRM.", file=sys.stderr)
        return 2
    store = Store(settings.db_path)
    try:
        report = run(settings, crm=_make_crm(settings), store=store, scrape_result=_scrape(settings))
    finally:
        store.close()

    print(
        f"Run {report.run_id}: {report.site_count} website locations "
        f"({report.directory_count} in directory; homepage-only: {', '.join(report.homepage_only) or 'none'}), "
        f"{report.crm_count} CRM accounts, {len(report.proposals)} proposals.\n"
    )
    counts = Counter((str(p.kind), p.priority) for p in report.proposals)
    kinds = sorted({k for k, _ in counts}, key=lambda k: (min(PRIORITY_ORDER[p] for kk, p in counts if kk == k), k))
    rows = [[k, *[counts.get((k, p), 0) or "" for p in PRIORITIES], sum(counts.get((k, p), 0) for p in PRIORITIES)] for k in kinds]
    rows.append(["TOTAL", *[sum(counts.get((k, p), 0) for k in kinds) for p in PRIORITIES], len(report.proposals)])
    _print_table(["kind", *PRIORITIES, "total"], rows)

    actionable = [p for p in report.proposals if p.actions]
    if actionable:
        print("\nTo review:")
        _print_table(
            ["priority", "kind", "account", "title"],
            [[p.priority, str(p.kind), p.subject_account_id or "(new)", p.title] for p in actionable],
        )
    s = report.summary
    print(f"\nStore: new={s.new} refreshed={s.refreshed} previously_decided={s.previously_decided} stale={s.stale}")
    print(f"Review with: bellhaven-sync review  (http://{settings.review_host}:{settings.review_port})")
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    settings = _settings(args)
    result = _scrape(settings)
    print(json.dumps([loc.to_dict() for loc in result.locations], indent=2, ensure_ascii=False))
    print(
        f"{len(result.locations)} locations; directory lists {result.directory_count} over "
        f"{getattr(result, 'directory_pages', '?')} pages; homepage-only: {', '.join(result.homepage_only_slugs) or 'none'}",
        file=sys.stderr,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .store import Store

    settings = _settings(args)
    if not settings.db_path.exists():
        print(f"No store yet at {settings.db_path}; run `bellhaven-sync run` first.")
        return 0
    store = Store(settings.db_path)
    try:
        counts = store.counts()
        print(f"Store: {settings.db_path}\n")
        _print_table(["status", "count"], [[k, v] for k, v in sorted(counts.items())] + [["TOTAL", sum(counts.values())]])
        pending = store.list(status="pending")
        if pending:
            by = Counter((str(sp.proposal.kind), sp.proposal.priority) for sp in pending)
            print("\nPending by kind:")
            _print_table(
                ["priority", "kind", "count"],
                [[p, k, n] for (k, p), n in sorted(by.items(), key=lambda kv: (PRIORITY_ORDER[kv[0][1]], kv[0][0]))],
            )
        runs = store.runs(limit=args.runs)
        if runs:
            print("\nRecent runs:")
            _print_table(
                ["run", "started", "finished", "site", "crm", "new", "refreshed", "decided", "stale", "notes"],
                [
                    [r["run_id"], r["started_at"], r["finished_at"] or "-", r["site_count"], r["crm_count"],
                     r["proposals_new"], r["proposals_refreshed"], r["proposals_previously_decided"],
                     r["proposals_stale"], (r["notes"] or "")[:80]]
                    for r in runs
                ],
            )
    finally:
        store.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    import uvicorn

    from .app.main import create_app
    from .crm import CRMClient
    from .store import Store

    settings = _settings(args)
    if not settings.token:
        print("BELLHAVEN_CRM_TOKEN is not set (env or .env); the review app needs it to apply changes.", file=sys.stderr)
        return 2
    app = create_app(settings, Store(settings.db_path), CRMClient(settings.base_url, settings.token))
    uvicorn.run(app, host=settings.review_host, port=settings.review_port)
    return 0


# --------------------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bellhaven-sync", description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLite store path (default: BELLHAVEN_DB_PATH or data/bellhaven_sync.db)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="scrape + reconcile + record proposals (read-only against the CRM)").set_defaults(func=cmd_run)
    sub.add_parser("scrape", help="scrape the website and print the locations as JSON").set_defaults(func=cmd_scrape)
    p_status = sub.add_parser("status", help="show proposal counts and recent runs")
    p_status.add_argument("--runs", type=int, default=5, help="how many recent runs to show")
    p_status.set_defaults(func=cmd_status)
    sub.add_parser("review", help="launch the local review app").set_defaults(func=cmd_review)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic streams
                pass
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
