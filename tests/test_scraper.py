"""Tests for the website scraper. No network except the single opt-in live smoke test."""

from __future__ import annotations

from collections import Counter

import httpx
import pytest

from bellhaven_sync.config import DEFAULT_BASE_URL
from bellhaven_sync.models import SiteLocation
from bellhaven_sync.scraper import (
    ScrapeError,
    ScrapeResult,
    http_fetcher,
    parse_detail_page,
    parse_directory_count,
    parse_directory_page,
    parse_homepage_slugs,
    scrape_site,
)

FINDLAY = "bellhaven-meadows-of-findlay"


# --------------------------------------------------------------------------- helpers


class RecordingFetcher:
    """Serves the captured site and records every requested path."""

    def __init__(self, pages: dict[str, str], fail_on: str | None = None):
        self.pages = pages
        self.fail_on = fail_on
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        self.calls.append(path)
        if path == self.fail_on:
            raise ConnectionError(f"boom fetching {path}")
        return self.pages[path]


def _directory_html(slugs: list[str], page: int, total: int, count: int) -> str:
    cards = "".join(
        f'<div class="card"><h3><a href="/communities/{s}">{s}</a></h3></div>' for s in slugs
    )
    return (
        '<html><body><nav><a href="/">Home</a><a href="/communities">Our Communities</a></nav>'
        f"<h1>Our Communities</h1><p>Page {page} of {total} · {count} communities listed</p>"
        f'<div class="cardgrid">{cards}</div>'
        f'<div class="pager"><span>Page {page} / {total}</span></div></body></html>'
    )


def _detail_html(name: str, city: str = "Springfield") -> str:
    return (
        f"<html><body><h1>{name}</h1><dl class='detail'>"
        f"<dt>Address</dt><dd>1 Main St<br>{city}, OH 45501</dd>"
        "<dt>Care Offerings</dt><dd><span class='badge'>Assisted Living</span></dd>"
        "<dt>Administrator</dt><dd>Pat Doe</dd><dt>Phone</dt><dd>(555) 555-0100</dd>"
        "</dl></body></html>"
    )


@pytest.fixture(scope="module")
def result(site_html) -> ScrapeResult:
    return scrape_site(lambda path: site_html[path])


@pytest.fixture(scope="module")
def by_slug(result) -> dict[str, SiteLocation]:
    return {loc.slug: loc for loc in result.locations}


# --------------------------------------------------------------------------- scrape_site on the capture


def test_scrapes_all_35_locations_sorted_and_unique(result):
    slugs = [loc.slug for loc in result.locations]
    assert len(slugs) == 35
    assert len(set(slugs)) == 35
    assert slugs == sorted(slugs)


def test_directory_count_and_pages(result):
    assert result.directory_count == 34
    assert result.directory_pages == 3


def test_homepage_only_location_is_flagged(result, by_slug):
    assert result.homepage_only_slugs == [FINDLAY]
    assert by_slug[FINDLAY].listed_in_directory is False
    assert sum(loc.listed_in_directory for loc in result.locations) == 34


def test_every_location_matches_expected_capture(by_slug, expected_locations):
    assert set(by_slug) == {row["slug"] for row in expected_locations}
    mismatches = []
    for row in expected_locations:
        loc = by_slug[row["slug"]]
        got = {
            "name": loc.name,
            "street": loc.street,
            "city": loc.city,
            "state": loc.state,
            "zip": loc.zip,
            "care": list(loc.care_offerings),
            "phone": loc.phone,
            "administrator": loc.administrator,
        }
        want = {k: row[k] for k in got}
        if got != want:
            mismatches.append((row["slug"], got, want))
    assert mismatches == []


def test_source_url_uses_base_url(site_html):
    res = scrape_site(lambda path: site_html[path], base_url="https://example.test/")
    for loc in res.locations:
        assert loc.source_url == f"https://example.test/communities/{loc.slug}"


def test_default_source_url(by_slug):
    assert by_slug["bellhaven-of-tiffin"].source_url == f"{DEFAULT_BASE_URL}/communities/bellhaven-of-tiffin"


def test_erie_has_two_care_offerings(by_slug):
    assert by_slug["bellhaven-shores-of-erie"].care_offerings == ("Assisted Living", "Memory Support")


def test_names_preserved_exactly_with_entities_unescaped(by_slug):
    grove = by_slug["bellhaven-rehabilitation-nursing-of-grove-city"]
    assert grove.name == "Bellhaven Rehabilitation & Nursing of Grove City"
    assert "&amp;" not in grove.name
    assert by_slug["the-arbors-at-bellhaven-dayton"].name == "The Arbors at Bellhaven - Dayton"
    assert by_slug["bellhaven-of-ashtabula"].care_offerings == ("Short-Term Rehabilitation & Nursing",)


@pytest.mark.parametrize(
    "slug,city",
    [
        ("bellhaven-of-chagrin-falls", "Chagrin Falls"),
        ("bellhaven-of-new-carlisle", "New Carlisle"),
        ("bellhaven-of-port-clinton", "Port Clinton"),
    ],
)
def test_multi_word_cities(by_slug, slug, city):
    assert by_slug[slug].city == city
    assert len(by_slug[slug].state) == 2
    assert len(by_slug[slug].zip) == 5


def test_about_text_mentions_acquisitions(result):
    assert "Harborview Care Group" in result.about_text
    assert "Cedar Trail" in result.about_text
    assert "<p>" not in result.about_text


def test_stops_on_page_counter_and_fetches_each_page_once(site_html):
    fetch = RecordingFetcher(site_html)
    scrape_site(fetch)
    assert "/communities?page=4" not in fetch.calls
    directory_calls = [c for c in fetch.calls if c.startswith("/communities?")]
    assert directory_calls == ["/communities?page=1", "/communities?page=2", "/communities?page=3"]
    detail_calls = [c for c in fetch.calls if c.startswith("/communities/")]
    assert len(detail_calls) == 35
    assert max(Counter(fetch.calls).values()) == 1
    assert "/" in fetch.calls and "/about" in fetch.calls


def test_failed_detail_fetch_raises_scrape_error(site_html):
    fetch = RecordingFetcher(site_html, fail_on="/communities/bellhaven-of-tiffin")
    with pytest.raises(ScrapeError, match="/communities/bellhaven-of-tiffin"):
        scrape_site(fetch)


def test_failed_homepage_fetch_raises_scrape_error(site_html):
    with pytest.raises(ScrapeError, match="'/'"):
        scrape_site(RecordingFetcher(site_html, fail_on="/"))


def test_scrape_error_is_runtime_error():
    assert issubclass(ScrapeError, RuntimeError)


# --------------------------------------------------------------------------- pagination edge cases


def _synthetic_site(directory: dict[int, str], homepage: str = "<html><body></body></html>") -> dict[str, str]:
    pages: dict[str, str] = {"/": homepage, "/about": "<html><body><p>About us</p></body></html>"}
    for n, html in directory.items():
        pages[f"/communities?page={n}"] = html
    return pages


def test_duplicate_slugs_across_pages_are_deduped():
    site = _synthetic_site(
        {
            1: _directory_html(["alpha", "bravo"], 1, 2, 3),
            2: _directory_html(["bravo", "charlie"], 2, 2, 3),
        }
    )
    for slug in ("alpha", "bravo", "charlie"):
        site[f"/communities/{slug}"] = _detail_html(slug.title())
    res = scrape_site(lambda p: site[p])
    assert [loc.slug for loc in res.locations] == ["alpha", "bravo", "charlie"]
    assert res.directory_count == 3
    assert res.directory_pages == 2
    assert res.homepage_only_slugs == []


def test_page_that_reports_wrong_number_raises():
    # a site that ignores ?page= and always serves page 1 must not be mistaken for success
    p1 = _directory_html(["alpha"], 1, 2, 2)
    site = _synthetic_site({1: p1, 2: p1})
    site["/communities/alpha"] = _detail_html("Alpha")
    with pytest.raises(ScrapeError, match="page=2"):
        scrape_site(lambda p: site[p])


def test_fewer_listings_than_stated_count_raises():
    site = _synthetic_site({1: _directory_html(["alpha"], 1, 1, 5)})
    site["/communities/alpha"] = _detail_html("Alpha")
    with pytest.raises(ScrapeError, match="5"):
        scrape_site(lambda p: site[p])


def test_missing_page_counter_raises():
    with pytest.raises(ScrapeError):
        parse_directory_page("<html><body><a href='/communities/alpha'>A</a></body></html>")


# --------------------------------------------------------------------------- parse_directory_page


def test_parse_directory_page_p3(site_html):
    slugs, current, total = parse_directory_page(site_html["/communities?page=3"])
    assert slugs == ["amberly-manor", "bellhaven-of-chesterton"]
    assert (current, total) == (3, 3)


def test_parse_directory_page_p1(site_html):
    slugs, current, total = parse_directory_page(site_html["/communities?page=1"])
    assert len(slugs) == 16
    assert slugs[0] == "bellhaven-of-maplewood"
    assert "bellhaven-rehabilitation-nursing-of-grove-city" in slugs
    assert (current, total) == (1, 3)


def test_parse_directory_page_slash_counter_only():
    html = '<p><a href="/communities/x-y">X</a></p><div class="pager"><span>Page 2 / 7</span></div>'
    assert parse_directory_page(html) == (["x-y"], 2, 7)


def test_parse_directory_count(site_html):
    for n in (1, 2, 3):
        assert parse_directory_count(site_html[f"/communities?page={n}"]) == 34
    assert parse_directory_count("<p>nothing here</p>") is None


# --------------------------------------------------------------------------- parse_homepage_slugs


def test_parse_homepage_slugs(site_html):
    assert parse_homepage_slugs(site_html["/"]) == [FINDLAY]


def test_parse_homepage_slugs_handles_absolute_links_and_ignores_others():
    html = (
        '<a href="/communities">Directory</a>'
        '<a href="https://analyst-assessment-production.up.railway.app/communities/foo-bar/">Foo</a>'
        '<a href="/communities/baz?ref=home#top">Baz</a>'
        '<a href="/communities/baz">Baz again</a>'
        '<a href="/about">About</a>'
        '<a href="/communities/foo-bar/photos">Not a detail page</a>'
    )
    assert parse_homepage_slugs(html) == ["foo-bar", "baz"]


# --------------------------------------------------------------------------- parse_detail_page


def test_parse_detail_page_erie(site_html):
    loc = parse_detail_page(site_html["/communities/bellhaven-shores-of-erie"], "bellhaven-shores-of-erie", "https://x.test")
    assert loc == SiteLocation(
        slug="bellhaven-shores-of-erie",
        name="Bellhaven Shores of Erie",
        street="4930 W Lake Rd",
        city="Erie",
        state="PA",
        zip="16505",
        care_offerings=("Assisted Living", "Memory Support"),
        phone="(614) 792-9343",
        administrator="Angela Quinn",
        source_url="https://x.test/communities/bellhaven-shores-of-erie",
        listed_in_directory=True,
    )


def test_parse_detail_page_is_order_and_whitespace_robust():
    html = """
    <html><body><h1>  Bellhaven   Rehab &amp; Nursing  </h1>
    <dl class="detail">
      <dt> Phone: </dt><dd> (419)&nbsp;555-0101 </dd>
      <dt>ADMINISTRATOR</dt><dd>
          Jo   Smith </dd>
      <dt>Care Offerings</dt><dd>
         <span class="badge"> Memory Support </span>
         <span class="badge">Short-Term Rehabilitation &amp; Nursing</span>
         <span class="badge">Memory Support</span>
      </dd>
      <dt>Address</dt><dd>
         12 North   Main Street <br/>
         Port   Clinton , OH 43452-1234
      </dd>
    </dl></body></html>
    """
    loc = parse_detail_page(html, "x", "https://b.test/")
    assert loc.name == "Bellhaven Rehab & Nursing"
    assert loc.street == "12 North Main Street"
    assert (loc.city, loc.state, loc.zip) == ("Port Clinton", "OH", "43452")
    assert loc.care_offerings == ("Memory Support", "Short-Term Rehabilitation & Nursing")
    assert loc.administrator == "Jo Smith"
    assert loc.phone == "(419) 555-0101"
    assert loc.source_url == "https://b.test/communities/x"


def test_parse_detail_page_multi_line_street():
    html = (
        "<h1>Two Line</h1><dl class='detail'><dt>Address</dt>"
        "<dd>100 Oak Ave<br>Suite 200<br>New Carlisle, OH 45344</dd></dl>"
    )
    loc = parse_detail_page(html, "two-line", "https://b.test")
    assert loc.street == "100 Oak Ave, Suite 200"
    assert (loc.city, loc.state, loc.zip) == ("New Carlisle", "OH", "45344")


def test_parse_detail_page_without_address_raises():
    html = "<h1>No Address</h1><dl class='detail'><dt>Phone</dt><dd>1</dd></dl>"
    with pytest.raises(ScrapeError, match="no-address"):
        parse_detail_page(html, "no-address", "https://b.test")


def test_parse_detail_page_with_unparseable_address_raises():
    html = "<h1>Bad</h1><dl class='detail'><dt>Address</dt><dd>somewhere nice</dd></dl>"
    with pytest.raises(ScrapeError, match="bad"):
        parse_detail_page(html, "bad", "https://b.test")


# --------------------------------------------------------------------------- http_fetcher (mock transport)


def test_http_fetcher_retries_5xx_and_timeouts_then_succeeds():
    seen: list[str] = []
    replies = iter(["timeout", 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        r = next(replies)
        if r == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(r, text="<html>ok</html>" if r == 200 else "busy")

    sleeps: list[float] = []
    fetch = http_fetcher("https://site.test/", retries=3, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert fetch("/communities?page=2") == "<html>ok</html>"
    assert seen == ["https://site.test/communities?page=2"] * 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_http_fetcher_gives_up_after_retries():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502)

    fetch = http_fetcher("https://site.test", retries=2, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(ScrapeError, match="/communities/foo"):
        fetch("/communities/foo")
    assert len(calls) == 3


def test_http_fetcher_does_not_retry_404():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="not found")

    fetch = http_fetcher("https://site.test", retries=3, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(ScrapeError, match="404"):
        fetch("/communities/missing")
    assert len(calls) == 1


# --------------------------------------------------------------------------- live smoke test


@pytest.mark.live
def test_live_site_scrape():
    with http_fetcher() as fetch:
        res = scrape_site(fetch)
    assert len(res.locations) == 35
    assert res.directory_count == 34
    assert res.directory_pages == 3
    assert FINDLAY in res.homepage_only_slugs
