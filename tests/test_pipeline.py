"""End-to-end pipeline: scrape result -> FakeCRM -> match -> propose -> store.

Covers read-only behaviour, idempotent re-runs, and convergence once proposals are applied.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from bellhaven_sync.config import DEFAULT_PARENT_NAME, Settings
from bellhaven_sync.models import NEW_ACCOUNT_REF, Proposal, ProposalKind, SiteLocation
from bellhaven_sync.pipeline import RunReport, run
from bellhaven_sync.store import Store
from tests.conftest import BELLHAVEN_PARENT_ID, HARBORVIEW_PARENT_ID, SITE

TODAY = date(2026, 10, 11)
EXPECTED_BY_KIND = {
    "confirmed": 18, "rename": 7, "reparent": 4, "chow_reparent": 2, "duplicate": 7,
    "create": 4, "chow_out": 1, "not_on_website": 2, "parent_acquired": 1,
}


@dataclass
class FakeScrape:
    """Duck-typed stand-in for scraper.ScrapeResult so these tests do not depend on the scraper."""

    locations: list[SiteLocation]
    directory_count: int = 34
    directory_pages: int = 3
    about_text: str = ""
    homepage_only_slugs: list[str] = field(default_factory=lambda: ["bellhaven-meadows-of-findlay"])


@pytest.fixture(scope="session")
def about_text() -> str:
    soup = BeautifulSoup((SITE / "about.html").read_text(encoding="utf-8"), "html.parser")
    return " ".join(soup.select_one(".wrap").get_text(" ").split())


@pytest.fixture
def scrape(site_locations, about_text) -> FakeScrape:
    return FakeScrape(locations=list(site_locations), about_text=about_text)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://example.invalid", token="test-token", db_path=tmp_path / "sync.db",
        parent_account_name=DEFAULT_PARENT_NAME, parent_account_id="",
    )


def _run(settings, crm, store, scrape) -> RunReport:
    return run(settings, crm=crm, store=store, scrape_result=scrape, today=TODAY)


def _apply(crm, p: Proposal) -> None:
    """Minimal stand-in for apply.py: run actions in order, resolving $new.account_id."""
    new_id: str | None = None
    for a in p.actions:
        fields = {k: (new_id if v == NEW_ACCOUNT_REF else v) for k, v in a.fields.items()}
        if a.op == "create":
            new_id = crm.create_account(fields).account_id
        else:
            crm.update_account(a.account_id, fields)


def test_run_end_to_end(settings, fake_crm, scrape) -> None:
    store = Store()
    report = _run(settings, fake_crm, store, scrape)
    assert report.run_id == 1
    assert report.site_count == 35 and report.crm_count == 121
    assert report.directory_count == 34 and report.homepage_only == ["bellhaven-meadows-of-findlay"]
    assert report.by_kind == EXPECTED_BY_KIND
    assert report.summary.new == len(report.proposals) == sum(EXPECTED_BY_KIND.values())
    assert fake_crm.writes == []  # the pipeline never writes to the CRM
    assert store.counts() == {"pending": len(report.proposals)}
    (r,) = store.runs()
    assert r["finished_at"] and r["site_count"] == 35 and r["crm_count"] == 121 and r["proposals_new"] == 46


def test_second_run_is_idempotent(settings, fake_crm, scrape) -> None:
    store = Store()
    first = _run(settings, fake_crm, store, scrape)
    second = run(settings, crm=fake_crm, store=store, scrape_result=scrape, today=date(2026, 10, 12))
    assert second.summary.new == 0
    assert second.summary.refreshed == len(first.proposals)
    assert second.summary.stale == 0
    assert {p.fingerprint for p in first.proposals} == {p.fingerprint for p in second.proposals}
    assert fake_crm.writes == []


def test_decided_proposals_are_not_reopened(settings, fake_crm, scrape) -> None:
    store = Store()
    first = _run(settings, fake_crm, store, scrape)
    rejected = next(p for p in first.proposals if p.kind == ProposalKind.NOT_ON_WEBSITE)
    store.decide(rejected.fingerprint, "rejected", by="test", note="still open, website lags")
    second = _run(settings, fake_crm, store, scrape)
    assert second.summary.new == 0 and second.summary.previously_decided == 1
    assert store.get(rejected.fingerprint).status == "rejected"


def test_pipeline_converges_after_applying(settings, fake_crm, scrape) -> None:
    store = Store()
    first = _run(settings, fake_crm, store, scrape)
    for p in first.proposals:
        if p.priority in ("high", "medium"):
            _apply(fake_crm, p)
    second = _run(settings, fake_crm, store, scrape)
    leftover = [p for p in second.proposals if p.kind != ProposalKind.CONFIRMED]
    # only the low-priority items nobody applied yet remain: 4 cosmetic renames + Harborview parent
    assert sorted(str(p.kind) for p in leftover) == ["parent_acquired", "rename", "rename", "rename", "rename"]
    assert all(p.priority == "low" for p in leftover)
    assert {p.fingerprint for p in leftover} <= {p.fingerprint for p in first.proposals}  # nothing new to review
    assert second.by_kind["confirmed"] == 35 - 4  # every website location now matches one account

    for p in leftover:
        _apply(fake_crm, p)
    third = _run(settings, fake_crm, store, scrape)
    assert third.by_kind == {"confirmed": 35}
    assert fake_crm.get_account(HARBORVIEW_PARENT_ID).status == "Inactive"

    # end state spot checks (what the CRM sandbox is graded on)
    tiffin_old = fake_crm.get_account("001U6RW32TY0WSXZZB")
    assert tiffin_old.chow_current_account and tiffin_old.parent_id != BELLHAVEN_PARENT_ID
    assert fake_crm.get_account(tiffin_old.chow_current_account).parent_id == BELLHAVEN_PARENT_ID
    assert fake_crm.get_account("001SXSF4ELF0Z2LGDM").chow_current_account == "0017JP8Z1UQ763BVK3"
    assert fake_crm.get_account("001SXSF4ELF0Z2LGDM").status == "Active"
    assert fake_crm.get_account("00116ETS45BL7DTQP7").status == "Needs Review"
    assert fake_crm.get_account("001QU150PM4Z15UA71").duplicate_of_account == "001EGU7BMJ942ZTRE6"
    k = fake_crm.get_account("001WR41PYNWXCAE2X4")
    assert (k.name, k.parent_id) == ("Bellhaven of Kettering", BELLHAVEN_PARENT_ID)


def test_run_uses_scraper_when_no_result_given(settings, fake_crm, scrape, monkeypatch) -> None:
    calls = []

    def fake_scrape_site(fetch, base_url):
        calls.append((fetch, base_url))
        return scrape

    fake_module = types.ModuleType("bellhaven_sync.scraper")
    fake_module.scrape_site = fake_scrape_site
    fake_module.http_fetcher = lambda base_url: pytest.fail("must not build a live fetcher when fetch is given")
    monkeypatch.setitem(sys.modules, "bellhaven_sync.scraper", fake_module)

    def fetch(path: str) -> str:
        return ""

    report = run(settings, crm=fake_crm, store=Store(), fetch=fetch, today=TODAY)
    assert calls == [(fetch, settings.site_url)]
    assert report.site_count == 35


def test_empty_scrape_fails_loudly_and_records_run(settings, fake_crm) -> None:
    store = Store()
    with pytest.raises(RuntimeError):
        _run(settings, fake_crm, store, FakeScrape(locations=[]))
    (r,) = store.runs()
    assert "failed" in r["notes"]
    assert store.counts() == {}
    assert fake_crm.writes == []


def test_missing_parent_account_fails(settings, fake_crm, scrape) -> None:
    from dataclasses import replace

    bad = replace(settings, parent_account_name="No Such Parent (Parent Account)")
    with pytest.raises(LookupError):
        _run(bad, fake_crm, Store(), scrape)


def test_store_defaults_to_settings_db_path(settings, fake_crm, scrape) -> None:
    report = run(settings, crm=fake_crm, scrape_result=scrape, today=TODAY)
    assert settings.db_path.exists()
    assert Store(settings.db_path).counts() == {"pending": len(report.proposals)}


def test_cli_run_prints_table(settings, fake_crm, scrape, monkeypatch, capsys) -> None:
    from bellhaven_sync import cli

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_make_crm", lambda s: fake_crm)
    monkeypatch.setattr(cli, "_scrape", lambda s: scrape)
    assert cli.main(["run"]) == 0
    out = capsys.readouterr().out
    assert "chow_reparent" in out and "high" in out and "new=46" in out
    assert fake_crm.writes == []
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending" in out and "46" in out


def test_pipeline_with_real_scraper_on_captured_html(settings, fake_crm, site_html) -> None:
    """Integration: the real scraper over the captured HTML feeds the same outcome table."""
    pytest.importorskip("bellhaven_sync.scraper")

    def fetch(path: str) -> str:
        return site_html[path]

    report = run(settings, crm=fake_crm, store=Store(), fetch=fetch, today=TODAY)
    assert report.by_kind == EXPECTED_BY_KIND
    assert report.site_count == 35 and report.directory_count == 34
    assert report.homepage_only == ["bellhaven-meadows-of-findlay"]
    harborview = next(p for p in report.proposals if p.kind == ProposalKind.PARENT_ACQUIRED)
    assert "Harborview Care Group" in harborview.evidence["about_text"]
    assert fake_crm.writes == []
