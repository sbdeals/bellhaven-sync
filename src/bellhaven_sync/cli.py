"""Command line: `bellhaven-sync run | scrape | status | review`.

`run` is read-only against the CRM (it only records proposals in the local store); the
only path that writes to the CRM is an Approve click in the review app.

`--snapshot DIR` (run / review / scrape) swaps the live CRM for `snapshot.SnapshotCRM(DIR)`
and the live website for the captured HTML in `DIR/../site` (when present), so the day-one
queue can be demoed after the live sandbox has been corrected. Approvals then write only to
`<db-dir>/snapshot_state.json`; the store defaults to `<db-dir>/snapshot.db`.
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
SNAPSHOT_BANNER = "SNAPSHOT MODE — no live CRM writes"


# --------------------------------------------------------------------------- seams


def _make_crm(settings: Settings) -> Any:
    from .crm import CRMClient

    return CRMClient(settings.base_url, settings.token)


def _scrape(settings: Settings, fetch: Any = None) -> Any:
    from .scraper import http_fetcher, scrape_site

    if fetch is not None:
        return scrape_site(fetch, settings.site_url)
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
    elif getattr(args, "snapshot", None):
        # keep snapshot proposals and decisions apart from the live store
        settings = replace(settings, db_path=settings.db_path.parent / "snapshot.db")
    return settings


def _snapshot_site_dir(snapshot_dir: Path) -> Path | None:
    site = snapshot_dir.resolve().parent / "site"
    return site if (site / "index.html").is_file() else None


def _snapshot_setup(args: argparse.Namespace, settings: Settings) -> tuple[Any, Any]:
    """(SnapshotCRM, fixture fetcher or None) for `--snapshot DIR`; prints the banner."""
    from .snapshot import SnapshotCRM, default_state_path, fixture_fetcher

    snap_dir = Path(args.snapshot)
    state = default_state_path(settings.db_path)
    crm = SnapshotCRM(snap_dir, state_path=state)
    site = _snapshot_site_dir(snap_dir)
    print(SNAPSHOT_BANNER)
    print(f"  CRM snapshot : {snap_dir}  (writes persist to {state})")
    print(f"  website      : {site if site else settings.site_url + ' (live, read-only)'}")
    print(f"  store        : {settings.db_path}\n")
    return crm, (fixture_fetcher(site) if site else None)


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
    if getattr(args, "snapshot", None):
        crm, fetch = _snapshot_setup(args, settings)
        scrape_result = _scrape(settings, fetch) if fetch is not None else _scrape(settings)
    else:
        if not settings.token:
            print("BELLHAVEN_CRM_TOKEN is not set (env or .env); cannot read the CRM.", file=sys.stderr)
            return 2
        crm, scrape_result = _make_crm(settings), _scrape(settings)
    store = Store(settings.db_path)
    try:
        report = run(settings, crm=crm, store=store, scrape_result=scrape_result)
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
    snap = f" --snapshot {args.snapshot} --db {settings.db_path}" if getattr(args, "snapshot", None) else ""
    print(f"Review with: bellhaven-sync review{snap}  (http://{settings.review_host}:{settings.review_port})")
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    from .snapshot import fixture_fetcher

    settings = _settings(args)
    site = _snapshot_site_dir(Path(args.snapshot)) if getattr(args, "snapshot", None) else None
    if site:
        print(f"{SNAPSHOT_BANNER} (captured website: {site})", file=sys.stderr)
    result = _scrape(settings, fixture_fetcher(site)) if site else _scrape(settings)
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
    if getattr(args, "snapshot", None):
        crm, _fetch = _snapshot_setup(args, settings)  # Approve writes to the snapshot state file only
    elif not settings.token:
        print("BELLHAVEN_CRM_TOKEN is not set (env or .env); the review app needs it to apply changes.", file=sys.stderr)
        return 2
    else:
        crm = CRMClient(settings.base_url, settings.token)
    app = create_app(settings, Store(settings.db_path), crm)
    uvicorn.run(app, host=settings.review_host, port=settings.review_port)
    return 0


# --------------------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    db_help = "SQLite store path (default: BELLHAVEN_DB_PATH or data/bellhaven_sync.db; data/snapshot.db with --snapshot)"
    parser = argparse.ArgumentParser(prog="bellhaven-sync", description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=db_help)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    # --db is accepted before or after the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help=db_help)
    snapshot = argparse.ArgumentParser(add_help=False)
    snapshot.add_argument(
        "--snapshot", metavar="DIR",
        help="use captured CRM JSON in DIR (accounts.json, contacts.json) and captured HTML in DIR/../site "
        "instead of the live CRM and website; writes go to <db-dir>/snapshot_state.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "run", parents=[common, snapshot], help="scrape + reconcile + record proposals (read-only against the CRM)"
    ).set_defaults(func=cmd_run)
    sub.add_parser(
        "scrape", parents=[common, snapshot], help="scrape the website and print the locations as JSON"
    ).set_defaults(func=cmd_scrape)
    p_status = sub.add_parser("status", parents=[common], help="show proposal counts and recent runs")
    p_status.add_argument("--runs", type=int, default=5, help="how many recent runs to show")
    p_status.set_defaults(func=cmd_status)
    sub.add_parser("review", parents=[common, snapshot], help="launch the local review app").set_defaults(func=cmd_review)
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
            try:  # UTF-8 even when piped on Windows (cp1252 otherwise mangles the banner / names)
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic streams
                pass
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
