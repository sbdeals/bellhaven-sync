"""Proposal building against the real 2026-10-11 captures: every row of the spec's
"Known data situations" table, the SOP branch, evidence shape and no-op suppression."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

import pytest
from bs4 import BeautifulSoup

from bellhaven_sync.match import match_all
from bellhaven_sync.models import NEW_ACCOUNT_REF, Account, Proposal, ProposalKind
from bellhaven_sync.proposals import build_proposals, resolve_parent_id
from tests.conftest import (
    BELLHAVEN_PARENT_ID,
    CEDAR_TRAIL_PARENT_ID,
    HARBORVIEW_PARENT_ID,
    SITE,
)

BH = BELLHAVEN_PARENT_ID
TODAY = date(2026, 10, 11)
STAMP = "[bellhaven-sync 2026-10-11]"
MUTABLE = {
    "name", "parent_id", "status", "note", "care_type", "phone", "billing_street", "billing_city",
    "billing_state", "billing_zip", "chow_current_account", "duplicate_of_account",
}
PATCH_ALLOWED = {"name", "parent_id", "status", "note", "chow_current_account", "duplicate_of_account"}
CREATE_FIELDS = {
    "name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip", "care_type",
    "phone", "status", "note",
}
ROLES = {"subject", "survivor", "loser", "related", "new_owner", "old", "parent"}

# Kettering: three copies, none under Bellhaven, no revenue, no phone/admin match, no contacts.
KETTERING_SURVIVOR = "001WR41PYNWXCAE2X4"  # "Kettering Care Centre" wins on name similarity
KETTERING_LOSERS = {"0016KTS1UAWBRXS09J", "001B7XZAA3AFALS9GP"}


@pytest.fixture(scope="session")
def about_text() -> str:
    soup = BeautifulSoup((SITE / "about.html").read_text(encoding="utf-8"), "html.parser")
    return " ".join(soup.select_one(".wrap").get_text(" ").split())


def _build(site_locations, accounts, contacts, about_text) -> list[Proposal]:
    matches = match_all(site_locations, accounts, contacts, BH)
    return build_proposals(matches, accounts, contacts, BH, about_text=about_text, today=TODAY)


@pytest.fixture(scope="module")
def proposals(site_locations, crm_accounts, crm_contacts, about_text) -> list[Proposal]:
    return _build(site_locations, crm_accounts, crm_contacts, about_text)


def _by_subject(props: list[Proposal]) -> dict[str, Proposal]:
    out: dict[str, Proposal] = {}
    for p in props:
        if p.subject_account_id:
            assert p.subject_account_id not in out, f"two proposals for {p.subject_account_id}"
            out[p.subject_account_id] = p
    return out


def _of_kind(props: list[Proposal], kind: ProposalKind) -> list[Proposal]:
    return [p for p in props if p.kind == kind]


def _for_slug(props: list[Proposal], slug: str, kind: ProposalKind | None = None) -> list[Proposal]:
    return [p for p in props if p.site_slug == slug and (kind is None or p.kind == kind)]


def _patched(accounts: list[Account], account_id: str, **changes) -> list[Account]:
    return [replace(a, **changes) if a.account_id == account_id else a for a in accounts]


# --------------------------------------------------------------------- parent


def test_resolve_parent_by_name_or_id(crm_accounts) -> None:
    assert resolve_parent_id(crm_accounts) == BH
    assert resolve_parent_id(crm_accounts, parent_id=HARBORVIEW_PARENT_ID) == HARBORVIEW_PARENT_ID
    with pytest.raises(LookupError):
        resolve_parent_id(crm_accounts, parent_name="Nobody (Parent Account)")
    with pytest.raises(LookupError):
        resolve_parent_id(crm_accounts, parent_id="001DOESNOTEXIST")


# ---------------------------------------------------------- full outcome table

EXPECTED_SITE_KINDS: dict[str, list[str]] = {
    "amberly-manor": ["create"],
    "bellhaven-at-sycamore-ridge": ["rename"],
    "bellhaven-at-union-square": ["create"],
    "bellhaven-court-of-altoona": ["confirmed"],
    "bellhaven-crossings-of-lima": ["reparent"],
    "bellhaven-gardens-of-monroe": ["confirmed", "duplicate", "duplicate"],
    "bellhaven-healthcare-centre-of-ashland": ["rename"],
    "bellhaven-meadows-of-findlay": ["reparent"],
    "bellhaven-of-ashtabula": ["confirmed"],
    "bellhaven-of-batavia": ["create"],
    "bellhaven-of-carlisle": ["create"],
    "bellhaven-of-chagrin-falls": ["rename"],
    "bellhaven-of-chesterton": ["rename"],
    "bellhaven-of-defiance": ["confirmed"],
    "bellhaven-of-goshen": ["confirmed"],
    "bellhaven-of-kalamazoo": ["confirmed"],
    "bellhaven-of-kettering": ["duplicate", "duplicate", "reparent"],
    "bellhaven-of-maplewood": ["confirmed"],
    "bellhaven-of-marietta": ["chow_reparent"],
    "bellhaven-of-marion": ["confirmed"],
    "bellhaven-of-meadville": ["confirmed"],
    "bellhaven-of-new-carlisle": ["confirmed"],
    "bellhaven-of-owosso": ["confirmed", "duplicate"],
    "bellhaven-of-port-clinton": ["confirmed", "duplicate"],
    "bellhaven-of-portsmouth": ["confirmed"],
    "bellhaven-of-saline": ["confirmed"],
    "bellhaven-of-tiffin": ["chow_reparent"],
    "bellhaven-of-wooster": ["confirmed"],
    "bellhaven-of-zanesville": ["reparent"],
    "bellhaven-rehabilitation-nursing-of-grove-city": ["rename"],
    "bellhaven-shores-of-erie": ["confirmed", "duplicate"],
    "bellhaven-terrace-of-akron": ["confirmed"],
    "bellhaven-willow-creek": ["rename"],
    "bellhaven-woods-of-toledo": ["confirmed"],
    "the-arbors-at-bellhaven-dayton": ["rename"],
}

EXPECTED_CRM_ONLY: dict[str, str] = {
    "001SXSF4ELF0Z2LGDM": "chow_out",         # Bellhaven of Sandusky
    "00116ETS45BL7DTQP7": "not_on_website",   # Bellhaven Care Center of Alliance
    "0016PVXH4B25HWR7QE": "not_on_website",   # Bellhaven of Coldwater
    HARBORVIEW_PARENT_ID: "parent_acquired",  # Harborview Care Group (Parent Account)
}


def test_full_expected_outcome_table(proposals, site_locations) -> None:
    got: dict[str, list[str]] = {}
    for p in proposals:
        if p.site_slug:
            got.setdefault(p.site_slug, []).append(str(p.kind))
    got = {k: sorted(v) for k, v in got.items()}
    print("\n--- outcome per website location ---")
    for s in sorted(site_locations, key=lambda s: s.slug):
        pairs = sorted(f"{p.kind}:{p.subject_account_id or '(new)'}" for p in _for_slug(proposals, s.slug))
        print(f"{s.slug:48s} {'  '.join(pairs)}")
    crm_only = {p.subject_account_id: str(p.kind) for p in proposals if not p.site_slug}
    print("--- CRM-only ---")
    for aid, kind in sorted(crm_only.items()):
        print(f"{aid:50s} {kind}")
    assert got == EXPECTED_SITE_KINDS
    assert crm_only == EXPECTED_CRM_ONLY


def test_kind_counts(proposals) -> None:
    counts: dict[str, int] = {}
    for p in proposals:
        counts[str(p.kind)] = counts.get(str(p.kind), 0) + 1
    assert counts == {
        "confirmed": 18, "rename": 7, "reparent": 4, "chow_reparent": 2, "duplicate": 7,
        "create": 4, "chow_out": 1, "not_on_website": 2, "parent_acquired": 1,
    }


# ------------------------------------------------------------------ confirmed


def test_confirmed_have_no_actions_and_low_priority(proposals) -> None:
    for p in _of_kind(proposals, ProposalKind.CONFIRMED):
        assert p.actions == [] and p.priority == "low"
        assert p.evidence["crm_accounts"][0]["parent_id"] == BH


def test_ashtabula_confirmed_with_address_difference_recorded(proposals) -> None:
    (p,) = _for_slug(proposals, "bellhaven-of-ashtabula")
    assert p.kind == ProposalKind.CONFIRMED and p.subject_account_id == "001NXP9X46CWEPSLSV"
    score = p.evidence["score"]
    assert score["address_differs"] is True and score["phone_match"] and score["admin_match"]
    assert any("PO Box 517" in n for n in p.evidence["notes"])


# --------------------------------------------------------------------- renames


@pytest.mark.parametrize(
    ("slug", "account_id", "new_name", "priority"),
    [
        ("bellhaven-rehabilitation-nursing-of-grove-city", "001TZCSTBZFM6K5BGN", "Bellhaven Rehabilitation & Nursing of Grove City", "low"),
        ("bellhaven-at-sycamore-ridge", "00191H7JR16471SZ34", "Bellhaven at Sycamore Ridge", "low"),
        ("the-arbors-at-bellhaven-dayton", "0010KJFP601YNTZVDA", "The Arbors at Bellhaven - Dayton", "low"),
        ("bellhaven-healthcare-centre-of-ashland", "001RJ0D7Z5MAZN5HX5", "Bellhaven Healthcare Centre of Ashland", "low"),
        ("bellhaven-of-chagrin-falls", "001RJU1X4NBWC1Q0G7", "Bellhaven of Chagrin Falls", "high"),
        ("bellhaven-willow-creek", "0017MN2JYAJBDS8WQZ", "Bellhaven Willow Creek", "high"),
        ("bellhaven-of-chesterton", "0013NUZQHQUEZ8DXEG", "Bellhaven of Chesterton", "high"),
    ],
)
def test_renames(proposals, slug, account_id, new_name, priority) -> None:
    (p,) = _for_slug(proposals, slug)
    assert p.kind == ProposalKind.RENAME and p.priority == priority and p.subject_account_id == account_id
    (a,) = p.actions
    assert a.op == "patch" and a.account_id == account_id
    assert set(a.fields) == {"name", "note"} and a.fields["name"] == new_name


# -------------------------------------------------------------------- reparent


@pytest.mark.parametrize(
    ("slug", "account_id", "fields"),
    [
        ("bellhaven-meadows-of-findlay", "001UKEFGADQ8YCZ4YM", {"parent_id": BH}),
        ("bellhaven-crossings-of-lima", "001LGFPBJY4N9MB6KL", {"parent_id": BH}),
        ("bellhaven-of-zanesville", "001H1JMVZWP46D5VUF", {"parent_id": BH, "name": "Bellhaven of Zanesville"}),
        ("bellhaven-of-kettering", KETTERING_SURVIVOR, {"parent_id": BH, "name": "Bellhaven of Kettering"}),
    ],
)
def test_reparents(proposals, slug, account_id, fields) -> None:
    (p,) = _for_slug(proposals, slug, ProposalKind.REPARENT)
    assert p.priority == "high" and p.subject_account_id == account_id
    (a,) = p.actions
    assert a.op == "patch" and a.account_id == account_id
    assert {k: v for k, v in a.fields.items() if k != "note"} == fields


def test_kettering_survivor_rule_is_documented(proposals) -> None:
    (p,) = _for_slug(proposals, "bellhaven-of-kettering", ProposalKind.REPARENT)
    assert any("name similarity" in n for n in p.evidence["notes"])
    roles = {a["account_id"]: a["role"] for a in p.evidence["crm_accounts"]}
    assert roles[KETTERING_SURVIVOR] == "subject"
    assert {aid for aid, r in roles.items() if r == "loser"} == KETTERING_LOSERS


# ----------------------------------------------------------------- SOP (CHOW)


@pytest.mark.parametrize(
    ("slug", "old_id", "old_name"),
    [("bellhaven-of-tiffin", "001U6RW32TY0WSXZZB", "Bellhaven of Tiffin"), ("bellhaven-of-marietta", "001A34WFSUYHCRBLFT", "Bellhaven of Marietta")],
)
def test_chow_reparent(proposals, site_locations, slug, old_id, old_name) -> None:
    site = next(s for s in site_locations if s.slug == slug)
    (p,) = _for_slug(proposals, slug)
    assert p.kind == ProposalKind.CHOW_REPARENT and p.priority == "high" and p.subject_account_id == old_id
    create, patch = p.actions
    assert create.op == "create" and set(create.fields) == CREATE_FIELDS
    assert create.fields["parent_id"] == BH and create.fields["name"] == site.name
    assert create.fields["billing_street"] == site.street and create.fields["billing_zip"] == site.zip
    assert create.fields["care_type"] == "Skilled Nursing" and create.fields["status"] == "Active"
    assert create.fields["phone"] == site.phone
    assert f"CHOW from {old_id} {old_name}" in create.fields["note"]
    assert patch.op == "patch" and patch.account_id == old_id
    assert set(patch.fields) == {"chow_current_account", "note"}  # parent/status/name untouched
    assert patch.fields["chow_current_account"] == NEW_ACCOUNT_REF
    roles = {a["account_id"]: a["role"] for a in p.evidence["crm_accounts"]}
    assert roles[old_id] == "old"


# ------------------------------------------------------------------ duplicates

EXPECTED_LOSERS = {
    "001JD2MWRA74LTSN24": "001UELXDAKFRKB8932",  # Harborview Nursing & Rehab of Port Clinton
    "001BLYF02K97SZLZHH": "001CVBBCSDM7YHN220",  # Harborview Shores of Erie
    "00159PL81N38KM4FHM": "001U1750VLVJAGG1S5",  # Monroe Gardens Care Center
    "0011AB44D05WLA9HTX": "001U1750VLVJAGG1S5",  # Cedar Trail of Monroe
    "001QU150PM4Z15UA71": "001EGU7BMJ942ZTRE6",  # second Owosso
    **{loser: KETTERING_SURVIVOR for loser in KETTERING_LOSERS},
}


def test_duplicates(proposals) -> None:
    dups = _of_kind(proposals, ProposalKind.DUPLICATE)
    assert {p.subject_account_id: p.actions[0].fields["duplicate_of_account"] for p in dups} == EXPECTED_LOSERS
    for p in dups:
        assert p.priority == "high"
        (a,) = p.actions
        assert a.account_id == p.subject_account_id
        assert a.fields["status"] == "Inactive" and set(a.fields) == {"duplicate_of_account", "status", "note"}
        roles = {x["account_id"]: x["role"] for x in p.evidence["crm_accounts"]}
        assert roles[p.subject_account_id] == "loser"
        assert roles[EXPECTED_LOSERS[p.subject_account_id]] == "survivor"
        assert "survivor" in p.evidence["rule"].lower()


def test_kettering_duplicate_rule_names_tiebreak(proposals) -> None:
    for p in _of_kind(proposals, ProposalKind.DUPLICATE):
        if p.subject_account_id in KETTERING_LOSERS:
            assert "name similarity" in p.evidence["rule"]


def test_duplicate_loser_with_revenue_and_ar_goes_to_needs_review(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    accts = _patched(crm_accounts, "001JD2MWRA74LTSN24", lifetime_revenue=5000, outstanding_ar=250)
    props = _by_subject(_build(site_locations, accts, crm_contacts, about_text))
    p = props["001JD2MWRA74LTSN24"]
    assert p.kind == ProposalKind.DUPLICATE
    assert p.actions[0].fields["status"] == "Needs Review"
    assert p.actions[0].fields["duplicate_of_account"] == "001UELXDAKFRKB8932"


# --------------------------------------------------------------------- creates


@pytest.mark.parametrize("slug", ["bellhaven-of-batavia", "bellhaven-of-carlisle", "amberly-manor", "bellhaven-at-union-square"])
def test_creates(proposals, site_locations, slug) -> None:
    site = next(s for s in site_locations if s.slug == slug)
    (p,) = _for_slug(proposals, slug)
    assert p.kind == ProposalKind.CREATE and p.priority == "high" and p.subject_account_id is None
    (a,) = p.actions
    assert a.op == "create" and set(a.fields) == CREATE_FIELDS
    f = a.fields
    assert (f["name"], f["parent_id"], f["billing_street"], f["billing_city"], f["billing_state"], f["billing_zip"]) == (
        site.name, BH, site.street, site.city, site.state, site.zip,
    )
    assert f["phone"] == site.phone and f["status"] == "Active"
    assert site.source_url in f["note"]
    for badge in site.care_offerings:
        assert badge in f["note"]


def test_create_care_type_is_first_badge_mapped(proposals) -> None:
    (p,) = _for_slug(proposals, "bellhaven-of-batavia")
    assert p.actions[0].fields["care_type"] == "Skilled Nursing"
    (p,) = _for_slug(proposals, "amberly-manor")
    assert p.actions[0].fields["care_type"] == "Assisted Living"


def test_union_square_related_account_in_evidence(proposals) -> None:
    (p,) = _for_slug(proposals, "bellhaven-at-union-square")
    roles = {a["account_id"]: a["role"] for a in p.evidence["crm_accounts"]}
    assert roles == {"001GNU41AVXZRLLJ9P": "related"}
    assert any("001GNU41AVXZRLLJ9P" in n and "not matched" in n for n in p.evidence["notes"])


def test_amberly_manor_name_collision_shown_not_matched(proposals) -> None:
    (p,) = _for_slug(proposals, "amberly-manor")
    roles = {a["account_id"]: a["role"] for a in p.evidence["crm_accounts"]}
    assert roles.get("0015D74ZLRY810RGY5") == "related"
    assert any("0015D74ZLRY810RGY5" in n and "CO" in n for n in p.evidence["notes"])


def test_carlisle_does_not_touch_new_carlisle(proposals) -> None:
    (p,) = _for_slug(proposals, "bellhaven-of-carlisle")
    assert all(a.account_id != "001AQGPZ5G5VRES536" for a in p.actions)


# ------------------------------------------------------------- CRM-only rules


def test_sandusky_chow_out(proposals) -> None:
    p = _by_subject(proposals)["001SXSF4ELF0Z2LGDM"]
    assert p.kind == ProposalKind.CHOW_OUT and p.priority == "high" and p.site_slug is None
    (a,) = p.actions
    assert a.account_id == "001SXSF4ELF0Z2LGDM"
    assert set(a.fields) == {"chow_current_account", "note"}
    assert a.fields["chow_current_account"] == "0017JP8Z1UQ763BVK3"
    roles = {x["account_id"]: x["role"] for x in p.evidence["crm_accounts"]}
    assert roles == {"001SXSF4ELF0Z2LGDM": "subject", "0017JP8Z1UQ763BVK3": "new_owner"}


@pytest.mark.parametrize("account_id", ["00116ETS45BL7DTQP7", "0016PVXH4B25HWR7QE"])
def test_not_on_website(proposals, account_id) -> None:
    p = _by_subject(proposals)[account_id]
    assert p.kind == ProposalKind.NOT_ON_WEBSITE and p.priority == "medium"
    (a,) = p.actions
    assert a.account_id == account_id and set(a.fields) == {"status", "note"} and a.fields["status"] == "Needs Review"


def test_not_on_website_skips_accounts_already_flagged(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    accts = _patched(crm_accounts, "00116ETS45BL7DTQP7", status="Needs Review")
    accts = _patched(accts, "0016PVXH4B25HWR7QE", status="Inactive")
    subjects = _by_subject(_build(site_locations, accts, crm_contacts, about_text))
    assert "00116ETS45BL7DTQP7" not in subjects and "0016PVXH4B25HWR7QE" not in subjects


def test_superseded_accounts_get_nothing(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    accts = _patched(crm_accounts, "001SXSF4ELF0Z2LGDM", chow_current_account="0017JP8Z1UQ763BVK3")
    accts = _patched(accts, "00116ETS45BL7DTQP7", duplicate_of_account="0016PVXH4B25HWR7QE")
    subjects = _by_subject(_build(site_locations, accts, crm_contacts, about_text))
    assert "001SXSF4ELF0Z2LGDM" not in subjects and "00116ETS45BL7DTQP7" not in subjects


# ------------------------------------------------------------ parent acquired


def test_harborview_parent_acquired(proposals, about_text) -> None:
    p = _by_subject(proposals)[HARBORVIEW_PARENT_ID]
    assert p.kind == ProposalKind.PARENT_ACQUIRED and p.priority == "low"
    (a,) = p.actions
    assert a.account_id == HARBORVIEW_PARENT_ID and set(a.fields) == {"status", "note"} and a.fields["status"] == "Inactive"
    assert "Harborview Care Group" in p.evidence["about_text"]
    assert "Harborview Care Group" in a.fields["note"]
    roles = {x["account_id"]: x["role"] for x in p.evidence["crm_accounts"]}
    assert roles[HARBORVIEW_PARENT_ID] == "subject"
    assert {aid for aid, r in roles.items() if r == "related"} == {
        "00159PL81N38KM4FHM", "001WR41PYNWXCAE2X4", "001LGFPBJY4N9MB6KL", "001JD2MWRA74LTSN24", "001BLYF02K97SZLZHH",
    }


def test_cedar_trail_not_acquired_even_if_named(site_locations, crm_accounts, crm_contacts) -> None:
    # Tiffin and Marietta stay under Cedar Trail by the CHOW SOP -> Cedar Trail is not retired.
    about = "We welcomed the Harborview Care Group family and communities from Cedar Trail Communities."
    subjects = _by_subject(_build(site_locations, crm_accounts, crm_contacts, about))
    assert subjects[HARBORVIEW_PARENT_ID].kind == ProposalKind.PARENT_ACQUIRED
    assert CEDAR_TRAIL_PARENT_ID not in subjects


def test_no_parent_acquired_without_about_text(site_locations, crm_accounts, crm_contacts) -> None:
    for about in (None, "", "Founded in 2003."):
        assert not _of_kind(_build(site_locations, crm_accounts, crm_contacts, about), ProposalKind.PARENT_ACQUIRED)


def test_parent_not_acquired_while_a_child_is_unmatched(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    extra = Account(
        account_id="001HVEXTRA000000001", name="Harborview Lakeside", parent_id=HARBORVIEW_PARENT_ID,
        billing_street="1 Elsewhere Rd", billing_city="Mentor", billing_state="OH", billing_zip="44060",
    )
    props = _build(site_locations, [*crm_accounts, extra], crm_contacts, about_text)
    assert not _of_kind(props, ProposalKind.PARENT_ACQUIRED)


# ----------------------------------------------------- notes and no-op rules


def test_notes_are_stamped_and_appended(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    accts = _patched(crm_accounts, "00116ETS45BL7DTQP7", note="Called 2026-09-01, no answer")
    p = _by_subject(_build(site_locations, accts, crm_contacts, about_text))["00116ETS45BL7DTQP7"]
    note = p.actions[0].fields["note"]
    assert note.startswith("Called 2026-09-01, no answer | " + STAMP)


def test_every_write_has_a_stamped_note_and_label(proposals) -> None:
    for p in proposals:
        for a in p.actions:
            assert a.label
            assert STAMP in a.fields["note"]
            assert set(a.fields) <= MUTABLE
            if a.op == "patch":
                assert a.account_id and set(a.fields) <= PATCH_ALLOWED  # never care_type/phone/address
            else:
                assert set(a.fields) == CREATE_FIELDS


def test_fingerprints_unique_and_note_independent(site_locations, crm_accounts, crm_contacts, about_text, proposals) -> None:
    assert len({p.fingerprint for p in proposals}) == len(proposals)
    later = build_proposals(
        match_all(site_locations, crm_accounts, crm_contacts, BH), crm_accounts, crm_contacts, BH,
        about_text=about_text, today=date(2026, 12, 25),
    )
    assert {p.fingerprint for p in later} == {p.fingerprint for p in proposals}


def test_already_applied_values_produce_no_change_proposals(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    # Lima already under Bellhaven -> confirmed, not reparent; Chesterton already renamed -> confirmed
    accts = _patched(crm_accounts, "001LGFPBJY4N9MB6KL", parent_id=BH)
    accts = _patched(accts, "0013NUZQHQUEZ8DXEG", name="Bellhaven of Chesterton")
    subjects = _by_subject(_build(site_locations, accts, crm_contacts, about_text))
    assert subjects["001LGFPBJY4N9MB6KL"].kind == ProposalKind.CONFIRMED
    assert subjects["0013NUZQHQUEZ8DXEG"].kind == ProposalKind.CONFIRMED


def test_reparent_for_account_without_revenue_and_ar_only(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    # Lima with open AR becomes a CHOW; Tiffin with AR cleared becomes a plain reparent
    accts = _patched(crm_accounts, "001LGFPBJY4N9MB6KL", outstanding_ar=100)
    accts = _patched(accts, "001U6RW32TY0WSXZZB", outstanding_ar=0)
    subjects = _by_subject(_build(site_locations, accts, crm_contacts, about_text))
    assert subjects["001LGFPBJY4N9MB6KL"].kind == ProposalKind.CHOW_REPARENT
    assert subjects["001U6RW32TY0WSXZZB"].kind == ProposalKind.REPARENT


# ------------------------------------------------------------- evidence shape


def test_evidence_shape(proposals) -> None:
    for p in proposals:
        ev = p.evidence
        assert set(ev) >= {"site", "crm_accounts", "contacts", "score", "rule", "notes"}, p.title
        assert isinstance(ev["rule"], str) and ev["rule"]
        assert isinstance(ev["notes"], list)
        ids = [a["account_id"] for a in ev["crm_accounts"]]
        assert len(ids) == len(set(ids))
        assert set(ev["contacts"]) == set(ids)
        for a in ev["crm_accounts"]:
            assert a["role"] in ROLES
        if p.site_slug:
            assert ev["site"]["slug"] == p.site_slug
        else:
            assert ev["site"] is None
        if p.kind in (ProposalKind.CONFIRMED, ProposalKind.RENAME, ProposalKind.REPARENT, ProposalKind.CHOW_REPARENT, ProposalKind.DUPLICATE):
            assert set(ev["score"]) >= {"total", "components", "street_match", "phone_match", "admin_match", "name_similarity", "address_differs"}
        if p.kind == ProposalKind.PARENT_ACQUIRED:
            assert ev["about_text"]
        else:
            assert ev.get("about_text") is None
        assert re.match(r"^\S", p.title) and p.summary


def test_findlay_homepage_only_is_noted(proposals) -> None:
    (p,) = _for_slug(proposals, "bellhaven-meadows-of-findlay")
    assert any("homepage" in n.lower() for n in p.evidence["notes"])


def test_null_money_fields_do_not_break_notes(site_locations, crm_accounts, crm_contacts, about_text) -> None:
    # the API's nulls arrive as "" via Account.from_api
    accts = _patched(crm_accounts, "00116ETS45BL7DTQP7", lifetime_revenue="", outstanding_ar="")
    p = _by_subject(_build(site_locations, accts, crm_contacts, about_text))["00116ETS45BL7DTQP7"]
    assert p.kind == ProposalKind.NOT_ON_WEBSITE and "$0" in p.evidence["notes"][0]
