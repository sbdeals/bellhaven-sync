"""One reconciliation run: scrape -> load CRM -> match -> propose -> store.

READ-ONLY against the CRM: only `list_accounts` and `list_contacts` are called here.
Writes happen exclusively in `apply.py`, triggered by a reviewer in the review app.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .config import Settings
from .crm import CRM
from .match import match_all
from .models import PRIORITY_ORDER, Proposal
from .proposals import build_proposals, resolve_parent_id
from .store import Store, UpsertSummary

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    run_id: int
    site_count: int
    crm_count: int
    proposals: list[Proposal]
    summary: UpsertSummary
    by_kind: dict[str, int]
    directory_count: int
    homepage_only: list[str]
    by_priority: dict[str, int] = field(default_factory=dict)


def _scrape(settings: Settings, fetch: Callable[[str], str] | None) -> Any:
    from .scraper import http_fetcher, scrape_site  # lazy: keeps matcher tests scraper-free

    if fetch is not None:
        return scrape_site(fetch, settings.site_url)
    fetcher = http_fetcher(settings.site_url)
    try:
        return scrape_site(fetcher, settings.site_url)
    finally:
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()


def run(
    settings: Settings,
    *,
    crm: CRM,
    store: Store | None = None,
    fetch: Callable[[str], str] | None = None,
    scrape_result: Any = None,
    today: date | None = None,
) -> RunReport:
    """Run the pipeline once and record proposals. `scrape_result` (anything shaped like
    `scraper.ScrapeResult`) skips scraping; otherwise the site is scraped with `fetch`
    (or a live HTTP fetcher)."""
    store = store if store is not None else Store(settings.db_path)
    run_id = store.start_run()
    site_count = crm_count = 0
    try:
        result = scrape_result if scrape_result is not None else _scrape(settings, fetch)
        locations = list(result.locations)
        site_count = len(locations)
        if not locations:
            raise RuntimeError("scrape returned no locations; refusing to reconcile against an empty website")
        directory_count = int(getattr(result, "directory_count", 0) or 0)
        homepage_only = list(getattr(result, "homepage_only_slugs", []) or [])
        listed = sum(1 for loc in locations if loc.listed_in_directory)
        if directory_count and listed != directory_count:
            log.warning("directory says %d communities but %d were scraped from it", directory_count, listed)

        accounts = crm.list_accounts()
        contacts = crm.list_contacts()
        crm_count = len(accounts)
        log.info("loaded %d CRM accounts and %d contacts", len(accounts), len(contacts))

        parent_id = resolve_parent_id(
            accounts, parent_id=settings.parent_account_id, parent_name=settings.parent_account_name
        )
        matches = match_all(locations, accounts, contacts, parent_id)
        proposals = build_proposals(
            matches, accounts, contacts, parent_id,
            about_text=getattr(result, "about_text", None), today=today,
        )
        summary = store.upsert_proposals(proposals, run_id)
    except Exception as exc:
        store.finish_run(
            run_id, site_count=site_count, crm_count=crm_count, summary=UpsertSummary(),
            notes=f"failed: {type(exc).__name__}: {exc}",
        )
        log.error("run %d failed: %s", run_id, exc)
        raise

    by_kind = dict(Counter(str(p.kind) for p in proposals))
    by_priority = dict(Counter(p.priority for p in proposals))
    store.finish_run(
        run_id, site_count=site_count, crm_count=crm_count, summary=summary,
        notes=", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())),
    )
    report = RunReport(
        run_id=run_id,
        site_count=site_count,
        crm_count=crm_count,
        proposals=proposals,
        summary=summary,
        by_kind=by_kind,
        directory_count=directory_count,
        homepage_only=homepage_only,
        by_priority=dict(sorted(by_priority.items(), key=lambda kv: PRIORITY_ORDER.get(kv[0], 9))),
    )
    log.info(
        "run %d: %d website locations (%d in directory, homepage-only: %s), %d CRM accounts -> %d proposals %s; "
        "store: new=%d refreshed=%d previously_decided=%d stale=%d",
        run_id, site_count, directory_count, ", ".join(homepage_only) or "none", crm_count, len(proposals),
        dict(sorted(by_kind.items())), summary.new, summary.refreshed, summary.previously_decided, summary.stale,
    )
    return report
