"""Scrape Bellhaven's public website into `SiteLocation` records.

Everything here except `http_fetcher` is a pure function over HTML. `scrape_site` takes a
`Fetcher` (site-relative path -> HTML) so tests can serve captured pages without a network.

Things the site does that this module defends against:
- Requesting a directory page past the end silently returns the last page again, so
  pagination stops on the "Page X of Y" counter, never on an "empty page".
- The homepage links to a community that is not in the directory, so every
  `/communities/<slug>` link on `/` is harvested too (`listed_in_directory=False`).
- Any fetch or parse failure raises `ScrapeError`: the daily job must fail loudly rather
  than silently produce fewer locations.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Callable
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from bellhaven_sync.config import DEFAULT_BASE_URL
from bellhaven_sync.models import SiteLocation

log = logging.getLogger(__name__)

Fetcher = Callable[[str], str]  # site-relative path like "/communities?page=2" -> HTML

USER_AGENT = "bellhaven-sync/0.1 (daily location sync)"
MAX_DIRECTORY_PAGES = 200  # sanity cap on the "Page X of Y" counter

_SLUG_PATH_RE = re.compile(r"^/communities/([A-Za-z0-9][A-Za-z0-9_-]*)/?$")
_PAGER_RE = re.compile(r"\bPage\s+(\d+)\s*(?:of|/)\s*(\d+)", re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d[\d,]*)\s+communit(?:y|ies)\s+listed", re.IGNORECASE)
_CITY_LINE_RE = re.compile(
    r"^(?P<city>.+?)\s*,\s*(?P<state>[A-Za-z]{2})\.?\s+(?P<zip>\d{5})(?:-\d{4})?$"
)
_ONE_LINE_ADDRESS_RE = re.compile(
    r"^(?P<street>.+?)\s*,\s*(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z]{2})\.?\s+(?P<zip>\d{5})(?:-\d{4})?$"
)
_BR_MARK = "\x00"


class ScrapeError(RuntimeError):
    """Raised when a page cannot be fetched or does not look like we expect."""


@dataclass
class ScrapeResult:
    locations: list[SiteLocation]  # all unique locations, sorted by slug
    directory_count: int  # the "N communities listed" number from the directory header
    directory_pages: int  # total pages per the "Page X of Y" counter
    about_text: str  # plain text of the /about page body (evidence for acquisitions)
    homepage_only_slugs: list[str]  # slugs linked from "/" but absent from the directory


# --------------------------------------------------------------------------- helpers


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _clean(text: str) -> str:
    """Collapse all whitespace (incl. non-breaking spaces) to single spaces and strip."""
    return " ".join(text.split())


def _slug_from_href(href: str) -> str | None:
    path = urlsplit(href.strip()).path
    m = _SLUG_PATH_RE.match(path)
    return m.group(1) if m else None


def _community_slugs(scopes: list[Tag] | list[BeautifulSoup]) -> list[str]:
    """Unique `/communities/<slug>` link targets in document order."""
    seen: set[str] = set()
    out: list[str] = []
    for scope in scopes:
        for a in scope.find_all("a", href=True):
            slug = _slug_from_href(str(a["href"]))
            if slug and slug not in seen:
                seen.add(slug)
                out.append(slug)
    return out


def _get(fetch: Fetcher, path: str) -> str:
    """Call the fetcher, turning any failure (or an empty body) into a ScrapeError."""
    try:
        html = fetch(path)
    except ScrapeError:
        raise
    except Exception as exc:  # noqa: BLE001 - the fetcher is pluggable; anything it raises is fatal
        raise ScrapeError(f"failed to fetch {path!r}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(html, str) or not html.strip():
        raise ScrapeError(f"empty response for {path!r}")
    return html


# --------------------------------------------------------------------------- page parsers


def parse_directory_page(html: str) -> tuple[list[str], int, int]:
    """Return (slugs on this page in order, current_page, total_pages).

    Accepts both counter styles the site uses ("Page 2 of 3" and "Page 2 / 3"). Raises
    ScrapeError when no counter is present, because without it pagination is unsafe.
    """
    soup = _soup(html)
    matches = [(int(a), int(b)) for a, b in _PAGER_RE.findall(soup.get_text(" "))]
    if not matches:
        raise ScrapeError("directory page has no 'Page X of Y' counter")
    current = matches[0][0]
    total = max(t for _, t in matches)
    if any(m != matches[0] for m in matches):
        log.warning("directory page counters disagree: %s; using page %d of %d", matches, current, total)
    scopes = soup.select(".cardgrid") or [soup]
    return _community_slugs(scopes), current, total


def parse_directory_count(html: str) -> int | None:
    """The 'N communities listed' number from a directory page header, if present."""
    m = _COUNT_RE.search(_soup(html).get_text(" "))
    return int(m.group(1).replace(",", "")) if m else None


def parse_homepage_slugs(html: str) -> list[str]:
    """Every `/communities/<slug>` detail link on a page (relative or absolute), in order."""
    return _community_slugs([_soup(html)])


def parse_page_text(html: str) -> str:
    """Readable text of a page's main content (no header/nav/footer/scripts), one block per line."""
    soup = _soup(html)
    for tag in soup.find_all(["script", "style", "noscript", "header", "nav", "footer"]):
        tag.decompose()
    container = soup.find("main") or soup.select_one(".wrap") or soup.body or soup
    lines = (_clean(line) for line in container.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def _dt_key(dt: Tag) -> str:
    return _clean(dt.get_text(" ")).rstrip(":").strip().lower()


def _detail_fields(dl: Tag) -> dict[str, list[Tag]]:
    """Map normalized <dt> label -> its <dd> elements (all dds up to the next dt)."""
    fields: dict[str, list[Tag]] = {}
    for dt in dl.find_all("dt"):
        dds: list[Tag] = []
        for sib in dt.find_next_siblings():
            if sib.name == "dt":
                break
            if sib.name == "dd":
                dds.append(sib)
        fields.setdefault(_dt_key(dt), []).extend(dds)
    return fields


def _lookup(fields: dict[str, list[Tag]], *labels: str) -> list[Tag] | None:
    for label in labels:
        if label in fields:
            return fields[label]
    for key, dds in fields.items():  # tolerate e.g. "Community Address", "Phone Number"
        if any(label in key for label in labels):
            return dds
    return None


def _text(dds: list[Tag] | None) -> str:
    return _clean(" ".join(dd.get_text(" ") for dd in dds)) if dds else ""


def _address_segments(dd: Tag) -> list[str]:
    for br in dd.find_all("br"):
        br.replace_with(_BR_MARK)
    raw = dd.get_text()
    segments = [_clean(s) for s in raw.split(_BR_MARK)]
    segments = [s for s in segments if s]
    if len(segments) < 2:  # no <br>: fall back to source line breaks
        segments = [s for s in (_clean(line) for line in raw.replace(_BR_MARK, "\n").splitlines()) if s]
    return segments


def _parse_address(dds: list[Tag], slug: str) -> tuple[str, str, str, str]:
    segments = [seg for dd in dds for seg in _address_segments(dd)]
    if len(segments) >= 2:
        m = _CITY_LINE_RE.match(segments[-1])
        if m:
            street = ", ".join(segments[:-1])
            return street, _clean(m["city"]), m["state"].upper(), m["zip"]
    m = _ONE_LINE_ADDRESS_RE.match(" ".join(segments)) if segments else None
    if m:
        return _clean(m["street"]), _clean(m["city"]), m["state"].upper(), m["zip"]
    raise ScrapeError(f"cannot parse address {segments!r} on detail page for {slug!r}")


def _care_offerings(dds: list[Tag] | None) -> tuple[str, ...]:
    if not dds:
        return ()
    items: list[str] = []
    for dd in dds:
        tags = dd.select(".badge") or dd.find_all("li")
        if tags:
            items.extend(_clean(t.get_text(" ")) for t in tags)
        else:
            items.extend(_clean(p) for p in re.split(r"[,;|·\n]", dd.get_text("\n")))
    return tuple(dict.fromkeys(i for i in items if i))


def _detail_name(soup: BeautifulSoup, dl: Tag) -> str:
    h1 = dl.find_previous("h1") or soup.find("h1")
    if h1 is not None:
        name = _clean(h1.get_text(" "))
        if name:
            return name
    if soup.title is not None:
        return _clean(soup.title.get_text().split(" — ")[0])
    return ""


def parse_detail_page(html: str, slug: str, base_url: str) -> SiteLocation:
    """Parse a `/communities/<slug>` page. Labels are looked up by <dt> text, not position."""
    soup = _soup(html)
    dl = soup.select_one("dl.detail")
    if dl is None:
        dl = next(
            (d for d in soup.find_all("dl") if "address" in {_dt_key(dt) for dt in d.find_all("dt")}),
            None,
        )
    if dl is None:
        raise ScrapeError(f"no <dl class='detail'> on detail page for {slug!r}")

    fields = _detail_fields(dl)
    address = _lookup(fields, "address", "location")
    if not address:
        raise ScrapeError(f"no Address on detail page for {slug!r}")
    street, city, state, zip_code = _parse_address(address, slug)

    name = _detail_name(soup, dl)
    if not name:
        raise ScrapeError(f"no community name (<h1>) on detail page for {slug!r}")

    care = _care_offerings(_lookup(fields, "care offerings", "care", "services"))
    administrator = _text(_lookup(fields, "administrator", "executive director"))
    phone = _text(_lookup(fields, "phone", "telephone"))
    for label, value in (("care offerings", care), ("administrator", administrator), ("phone", phone)):
        if not value:
            log.warning("detail page for %r has no %s", slug, label)

    return SiteLocation(
        slug=slug,
        name=name,
        street=street,
        city=city,
        state=state,
        zip=zip_code,
        care_offerings=care,
        phone=phone,
        administrator=administrator,
        source_url=f"{base_url.rstrip('/')}/communities/{slug}",
        listed_in_directory=True,
    )


# --------------------------------------------------------------------------- orchestration


def _walk_directory(fetch: Fetcher) -> tuple[list[str], int, int]:
    """Fetch /communities?page=1..Y, stopping on the counter. Returns (slugs, count, pages)."""
    slugs: list[str] = []
    seen: set[str] = set()
    count: int | None = None
    page, total = 1, 1
    while page <= total:
        path = f"/communities?page={page}"
        html = _get(fetch, path)
        try:
            page_slugs, current, page_total = parse_directory_page(html)
        except ScrapeError as exc:
            raise ScrapeError(f"{path}: {exc}") from exc
        if current != page:
            raise ScrapeError(
                f"{path} reported itself as page {current} of {page_total}; pagination is not behaving as expected"
            )
        if page == 1:
            total = page_total
        elif page_total != total:
            log.warning("%s reports %d pages (page 1 said %d); using the larger", path, page_total, total)
            total = max(total, page_total)
        if total > MAX_DIRECTORY_PAGES:
            raise ScrapeError(f"{path} claims {total} directory pages; refusing (cap {MAX_DIRECTORY_PAGES})")
        if count is None:
            count = parse_directory_count(html)

        new = [s for s in page_slugs if s not in seen]
        if len(new) < len(page_slugs):
            log.warning("%s repeats %d slug(s) already seen on earlier pages", path, len(page_slugs) - len(new))
        seen.update(new)
        slugs.extend(new)
        page += 1

    if count is None:
        log.warning("directory header has no 'N communities listed'; using %d unique listings", len(slugs))
        count = len(slugs)
    elif len(slugs) < count:
        raise ScrapeError(f"directory says {count} communities listed but only {len(slugs)} were found across {total} page(s)")
    elif len(slugs) > count:
        log.warning("directory says %d communities listed but %d unique listings were found", count, len(slugs))
    return slugs, count, total


def scrape_site(fetch: Fetcher, base_url: str = DEFAULT_BASE_URL) -> ScrapeResult:
    """Scrape the directory, homepage, about page and every detail page.

    `base_url` is only used to build each location's `source_url`; `fetch` decides where
    pages actually come from. Raises ScrapeError on any fetch or parse failure.
    """
    directory_slugs, directory_count, directory_pages = _walk_directory(fetch)
    listed = set(directory_slugs)

    homepage_slugs = parse_homepage_slugs(_get(fetch, "/"))
    homepage_only = sorted(s for s in homepage_slugs if s not in listed)
    if homepage_only:
        log.info("homepage links to %d community(ies) not in the directory: %s", len(homepage_only), homepage_only)

    about_text = parse_page_text(_get(fetch, "/about"))

    locations: list[SiteLocation] = []
    for slug in sorted(listed | set(homepage_only)):
        path = f"/communities/{slug}"
        html = _get(fetch, path)
        try:
            loc = parse_detail_page(html, slug, base_url)
        except ScrapeError as exc:
            raise ScrapeError(f"{path}: {exc}") from exc
        if slug not in listed:
            loc = replace(loc, listed_in_directory=False)
        locations.append(loc)

    log.info(
        "scraped %d locations (%d listed in a %d-page directory, %d homepage-only)",
        len(locations),
        directory_count,
        directory_pages,
        len(homepage_only),
    )
    return ScrapeResult(
        locations=locations,
        directory_count=directory_count,
        directory_pages=directory_pages,
        about_text=about_text,
        homepage_only_slugs=homepage_only,
    )


# --------------------------------------------------------------------------- HTTP


class HttpFetcher:
    """httpx-backed `Fetcher`. Retries 5xx/429, timeouts and connection errors with
    exponential backoff; other 4xx fail immediately. Usable as a context manager."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = max(0, retries)
        self.backoff = backoff
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    def __call__(self, path: str) -> str:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}/{path.lstrip('/')}"
        attempts = self.retries + 1
        error = ""
        for attempt in range(attempts):
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:  # timeouts, connection resets, DNS, ...
                error = f"{type(exc).__name__}: {exc}"
            except httpx.HTTPError as exc:  # e.g. too many redirects: not worth retrying
                raise ScrapeError(f"GET {path!r} failed: {type(exc).__name__}: {exc}") from exc
            else:
                if resp.status_code < 400:
                    return resp.text
                if resp.status_code < 500 and resp.status_code != 429:
                    raise ScrapeError(f"GET {path!r} failed with HTTP {resp.status_code}")
                error = f"HTTP {resp.status_code}"
            if attempt + 1 < attempts:
                delay = self.backoff * (2**attempt)
                log.warning("GET %s failed (%s); retry %d/%d in %.1fs", path, error, attempt + 1, self.retries, delay)
                self._sleep(delay)
        raise ScrapeError(f"GET {path!r} failed after {attempts} attempt(s): {error}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpFetcher":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def http_fetcher(
    base_url: str = DEFAULT_BASE_URL,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    transport: httpx.BaseTransport | None = None,
) -> HttpFetcher:
    """A `Fetcher` for the live site. `retries` is the number of extra attempts after the
    first. `sleep` and `transport` exist so tests can run without waiting or a network."""
    return HttpFetcher(base_url, timeout=timeout, retries=retries, backoff=backoff, sleep=sleep, transport=transport)
