"""Candidate blocking, scoring, decisions, duplicate clusters and survivor ordering,
run against the real 2026-10-11 captures."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bellhaven_sync.match import (
    Candidate,
    block_candidates,
    contacts_by_account,
    eligible_accounts,
    match_all,
    match_location,
    score_account,
    survivor_sort_key,
)
from bellhaven_sync.models import Account, Contact, SiteLocation
from tests.conftest import BELLHAVEN_PARENT_ID, CEDAR_TRAIL_PARENT_ID, HARBORVIEW_PARENT_ID

BH = BELLHAVEN_PARENT_ID


@pytest.fixture(scope="module")
def by_slug(site_locations) -> dict[str, SiteLocation]:
    return {s.slug: s for s in site_locations}


@pytest.fixture(scope="module")
def eligible(crm_accounts) -> list[Account]:
    return eligible_accounts(crm_accounts)


@pytest.fixture(scope="module")
def contacts_map(crm_contacts) -> dict[str, list[Contact]]:
    return contacts_by_account(crm_contacts)


@pytest.fixture(scope="module")
def results(site_locations, crm_accounts, crm_contacts):
    return {m.site.slug: m for m in match_all(site_locations, crm_accounts, crm_contacts, BH)}


def _ids(cands: list[Candidate]) -> list[str]:
    return [c.account.account_id for c in cands]


# ------------------------------------------------------------------ blocking


def test_eligible_excludes_parents_and_superseded(crm_accounts) -> None:
    by_id = {a.account_id: a for a in crm_accounts}
    superseded = replace(by_id["001YQH5RQ9VL8C42GU"], duplicate_of_account="001X")
    chowed = replace(by_id["001E49NRPBQGBELSHY"], chow_current_account="001Y")
    by_id[superseded.account_id] = superseded
    by_id[chowed.account_id] = chowed
    ids = {a.account_id for a in eligible_accounts(list(by_id.values()))}
    assert BH not in ids and HARBORVIEW_PARENT_ID not in ids and CEDAR_TRAIL_PARENT_ID not in ids
    assert superseded.account_id not in ids and chowed.account_id not in ids
    assert len(ids) == 121 - 6 - 2


def test_blocking_same_state_and_city_or_zip(by_slug, eligible) -> None:
    names = lambda slug: {a.name for a in block_candidates(by_slug[slug], eligible)}  # noqa: E731
    assert "Bellhaven of New Carlisle" not in names("bellhaven-of-carlisle")  # different state AND city
    assert names("bellhaven-of-carlisle") == set()
    assert names("amberly-manor") == set()  # CRM "Amberly Manor" is in Colorado Springs, CO
    assert "Union Square Senior Living" in names("bellhaven-at-union-square")
    assert {"Bellhaven of Maplewood", "Maplewood Senior Care Center"} <= names("bellhaven-of-maplewood")
    # Portsmouth: CRM zip 45626 vs site 45662, still blocked by the city
    assert "Bellhaven of Portsmouth" in names("bellhaven-of-portsmouth")


# ------------------------------------------------------------------- scoring


def test_score_components_chesterton(by_slug, eligible, contacts_map) -> None:
    acct = next(a for a in eligible if a.account_id == "0013NUZQHQUEZ8DXEG")
    s = score_account(by_slug["bellhaven-of-chesterton"], acct, contacts_map.get(acct.account_id, []))
    assert s.street_match and s.phone_match and s.admin_match and s.zip_match and s.city_match
    assert s.components["street"] == 45 and s.components["zip"] == 10 and s.components["city"] == 10
    assert s.components["phone"] == 20 and s.components["admin"] == 15 and s.components["locality"] == 10
    assert 0 < s.components["name"] <= 25
    assert s.total == 100  # capped
    d = s.to_dict()
    assert set(d) >= {"total", "components", "street_match", "phone_match", "admin_match", "name_similarity", "address_differs"}


def test_ashtabula_po_box_matches_on_phone_and_admin(results) -> None:
    m = results["bellhaven-of-ashtabula"]
    assert _ids(m.matched) == ["001NXP9X46CWEPSLSV"]
    s = m.survivor.score
    assert not s.street_match and s.phone_match and s.admin_match
    assert s.address_differs is True
    assert s.total >= 60


def test_union_square_is_not_matched(results) -> None:
    m = results["bellhaven-at-union-square"]
    assert m.matched == []
    jp = next(c for c in m.candidates if c.account.account_id == "001GNU41AVXZRLLJ9P")
    assert not (jp.score.street_match or jp.score.phone_match or jp.score.admin_match)
    assert jp.score.name_similarity < 90
    assert _ids(m.related) == ["001GNU41AVXZRLLJ9P"]


@pytest.mark.parametrize("slug", ["amberly-manor", "bellhaven-of-batavia", "bellhaven-of-carlisle", "bellhaven-at-union-square"])
def test_no_match_locations(results, slug) -> None:
    assert results[slug].matched == []


def test_maplewood_only_matches_bellhaven_copy(results) -> None:
    m = results["bellhaven-of-maplewood"]
    assert _ids(m.matched) == ["001L3LGR6W69J8DYYV"]


def test_every_other_location_matches(results) -> None:
    unmatched = {slug for slug, m in results.items() if not m.matched}
    assert unmatched == {"amberly-manor", "bellhaven-at-union-square", "bellhaven-of-batavia", "bellhaven-of-carlisle"}
    assert len(results) == 35


# ---------------------------------------------------------- duplicate clusters


@pytest.mark.parametrize(
    ("slug", "survivor", "losers"),
    [
        ("bellhaven-of-port-clinton", "001UELXDAKFRKB8932", {"001JD2MWRA74LTSN24"}),
        ("bellhaven-shores-of-erie", "001CVBBCSDM7YHN220", {"001BLYF02K97SZLZHH"}),
        ("bellhaven-gardens-of-monroe", "001U1750VLVJAGG1S5", {"00159PL81N38KM4FHM", "0011AB44D05WLA9HTX"}),
        ("bellhaven-of-owosso", "001EGU7BMJ942ZTRE6", {"001QU150PM4Z15UA71"}),
        # Kettering: no Bellhaven parent, no revenue, no phone/admin match, no contacts on any
        # copy -> decided by name similarity: "Kettering Care Centre" (65.0) beats the others (64.3)
        ("bellhaven-of-kettering", "001WR41PYNWXCAE2X4", {"0016KTS1UAWBRXS09J", "001B7XZAA3AFALS9GP"}),
    ],
)
def test_duplicate_clusters(results, slug, survivor, losers) -> None:
    m = results[slug]
    assert m.is_duplicate_cluster
    assert m.survivor.account.account_id == survivor
    assert set(_ids(m.losers)) == losers
    assert m.survivor_rule  # explanation recorded for the evidence


def test_owosso_survivor_is_phone_and_admin_match(results) -> None:
    s = results["bellhaven-of-owosso"].survivor.score
    assert s.phone_match and s.admin_match


def test_single_matches_are_not_clusters(results) -> None:
    clusters = {slug for slug, m in results.items() if m.is_duplicate_cluster}
    assert clusters == {
        "bellhaven-of-port-clinton", "bellhaven-shores-of-erie", "bellhaven-gardens-of-monroe",
        "bellhaven-of-owosso", "bellhaven-of-kettering",
    }


# --------------------------------------------------------- survivor ordering


def _site() -> SiteLocation:
    return SiteLocation(
        slug="bellhaven-of-testville", name="Bellhaven of Testville", street="1 Main St", city="Testville",
        state="OH", zip="44000", care_offerings=("Assisted Living",), phone="(555) 111-2222",
        administrator="Pat Admin", source_url="https://x/communities/bellhaven-of-testville",
    )


def _acct(aid: str, **kw) -> Account:
    base = dict(
        account_id=aid, name="Testville Home", parent_id="", billing_street="1 Main Street",
        billing_city="Testville", billing_state="OH", billing_zip="44000", phone="(555) 999-0000",
    )
    base.update(kw)
    return Account(**base)


def _ordered(accounts: list[Account], contacts: dict[str, list[Contact]] | None = None) -> list[str]:
    site = _site()
    contacts = contacts or {}
    cands = [
        Candidate(a, score_account(site, a, contacts.get(a.account_id, [])), len(contacts.get(a.account_id, [])))
        for a in accounts
    ]
    cands.sort(key=lambda c: survivor_sort_key(c, BH))
    return [c.account.account_id for c in cands]


def test_survivor_order_bellhaven_parent_beats_revenue() -> None:
    assert _ordered([_acct("001A", lifetime_revenue=5000), _acct("001B", parent_id=BH)])[0] == "001B"


def test_survivor_order_revenue_beats_phone() -> None:
    assert _ordered([_acct("001A", phone="(555) 111-2222"), _acct("001B", lifetime_revenue=10)])[0] == "001B"


def test_survivor_order_phone_beats_admin() -> None:
    contacts = {"001A": [Contact("c1", "001A", "Pat Admin", "Administrator")]}
    assert _ordered([_acct("001A"), _acct("001B", phone="555-111-2222")], contacts)[0] == "001B"


def test_survivor_order_admin_beats_name() -> None:
    contacts = {"001B": [Contact("c1", "001B", "Pat Admin", "Administrator")]}
    assert _ordered([_acct("001A", name="Bellhaven of Testville"), _acct("001B")], contacts)[0] == "001B"


def test_survivor_order_inactive_contacts_do_not_count_as_admin_match() -> None:
    contacts = {"001A": [Contact("c1", "001A", "Pat Admin", "Administrator", is_active=False)]}
    assert _ordered([_acct("001A"), _acct("001B", name="Bellhaven of Testville")], contacts)[0] == "001B"


def test_survivor_order_name_then_contacts_then_id() -> None:
    assert _ordered([_acct("001A"), _acct("001B", name="Bellhaven of Testville")])[0] == "001B"
    contacts = {"001B": [Contact("c1", "001B", "Someone Else")]}
    assert _ordered([_acct("001A"), _acct("001B")], contacts)[0] == "001B"
    assert _ordered([_acct("001C"), _acct("001A"), _acct("001B")]) == ["001A", "001B", "001C"]


# ----------------------------------------------------------- fallback rules


def test_name_only_fallback_needs_some_non_name_evidence() -> None:
    site = _site()
    # same name, same city, different street, different phone, no contacts -> no match
    other = _acct("001A", name="Bellhaven of Testville", billing_street="99 Other Rd")
    m = match_location(site, [other], {}, BH)
    assert m.matched == []
    assert _ids(m.related) == ["001A"]


def test_name_fallback_with_phone_match_flags_address_differs() -> None:
    site = _site()
    acct = _acct("001A", name="Bellhaven of Testville", billing_street="PO Box 9", phone="555 111 2222")
    m = match_location(site, [acct], {}, BH)
    assert _ids(m.matched) == ["001A"]
    assert m.survivor.score.address_differs


def test_account_claimed_by_two_locations_goes_to_best_score() -> None:
    a = _site()
    b = replace(a, slug="bellhaven-of-testville-2", name="Bellhaven Testville Annex", phone="(555) 000-0000")
    acct = _acct("001A", name="Bellhaven of Testville", phone="(555) 111-2222")
    res = {m.site.slug: m for m in match_all([a, b], [acct], [], BH)}
    assert _ids(res["bellhaven-of-testville"].matched) == ["001A"]
    assert res["bellhaven-of-testville-2"].matched == []
