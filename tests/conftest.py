from __future__ import annotations

import json
from pathlib import Path

import pytest

from bellhaven_sync.models import Account, Contact, SiteLocation

FIXTURES = Path(__file__).parent / "fixtures"
SITE = FIXTURES / "site"

BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"
HARBORVIEW_PARENT_ID = "001FJZYHR7MLFMNPLL"
CEDAR_TRAIL_PARENT_ID = "001FWSQ30SFW6S7604"
MILLSTONE_PARENT_ID = "001YRHHXQ5HJ0TCL2U"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: hits the real CRM sandbox / website (opt in with -m live)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="live test; run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def site_html() -> dict[str, str]:
    """Path -> HTML for every captured page, keyed the way the scraper requests them."""
    pages: dict[str, str] = {
        "/": (SITE / "index.html").read_text(encoding="utf-8"),
        "/about": (SITE / "about.html").read_text(encoding="utf-8"),
        "/communities": (SITE / "communities_p1.html").read_text(encoding="utf-8"),
        "/communities?page=1": (SITE / "communities_p1.html").read_text(encoding="utf-8"),
        "/communities?page=2": (SITE / "communities_p2.html").read_text(encoding="utf-8"),
        "/communities?page=3": (SITE / "communities_p3.html").read_text(encoding="utf-8"),
        # the real site returns the last page again for out-of-range pages
        "/communities?page=4": (SITE / "communities_p3.html").read_text(encoding="utf-8"),
    }
    for f in (SITE / "communities").glob("*.html"):
        pages[f"/communities/{f.stem}"] = f.read_text(encoding="utf-8")
    return pages


@pytest.fixture(scope="session")
def expected_locations() -> list[dict]:
    return json.loads((FIXTURES / "site_locations_expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def crm_accounts() -> list[Account]:
    data = json.loads((FIXTURES / "crm" / "accounts.json").read_text(encoding="utf-8"))["data"]
    return [Account.from_api(d) for d in data]


@pytest.fixture(scope="session")
def crm_contacts() -> list[Contact]:
    data = json.loads((FIXTURES / "crm" / "contacts.json").read_text(encoding="utf-8"))["data"]
    return [Contact.from_api(d) for d in data]


@pytest.fixture(scope="session")
def site_locations(expected_locations) -> list[SiteLocation]:
    """The 35 website locations as SiteLocation objects (built from the parsed capture so
    matcher tests do not depend on the scraper)."""
    out = []
    for r in expected_locations:
        out.append(
            SiteLocation(
                slug=r["slug"],
                name=r["name"],
                street=r["street"],
                city=r["city"],
                state=r["state"],
                zip=r["zip"],
                care_offerings=tuple(r["care"]),
                phone=r["phone"],
                administrator=r["administrator"],
                source_url=f"https://analyst-assessment-production.up.railway.app/communities/{r['slug']}",
                listed_in_directory=(r["slug"] != "bellhaven-meadows-of-findlay"),
            )
        )
    return out


@pytest.fixture
def fake_crm():
    from tests.fakes import FakeCRM

    return FakeCRM()
