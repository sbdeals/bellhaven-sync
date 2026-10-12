from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():  # starlette warns that httpx-based TestClient is deprecated
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from bellhaven_sync.app.main import create_app
from bellhaven_sync.config import Settings
from bellhaven_sync.models import NEW_ACCOUNT_REF, Action, Proposal, ProposalKind
from bellhaven_sync.store import Store
from tests.conftest import BELLHAVEN_PARENT_ID
from tests.fakes import FakeCRM

FIXTURES = Path(__file__).parent / "fixtures"
_ACCOUNTS = {a["account_id"]: a for a in json.loads((FIXTURES / "crm" / "accounts.json").read_text(encoding="utf-8"))["data"]}
_CONTACTS = json.loads((FIXTURES / "crm" / "contacts.json").read_text(encoding="utf-8"))["data"]

GROVE_CITY = "001TZCSTBZFM6K5BGN"
TIFFIN_OLD = "001U6RW32TY0WSXZZB"
PORT_CLINTON_BH = "001UELXDAKFRKB8932"
PORT_CLINTON_HV = "001JD2MWRA74LTSN24"
WOOSTER = "001W5G121A7SMRP58M"
SITE = "https://analyst-assessment-production.up.railway.app"


def site(slug: str, name: str, street: str, city: str, zip_: str, phone: str, admin: str, care=("Short-Term Rehabilitation & Nursing",)) -> dict[str, Any]:
    return {
        "slug": slug, "name": name, "street": street, "city": city, "state": "OH", "zip": zip_,
        "care_offerings": list(care), "phone": phone, "administrator": admin,
        "source_url": f"{SITE}/communities/{slug}", "listed_in_directory": True,
    }


def evidence(site_rec: dict | None, roles: dict[str, str], score: dict | None, rule: str, notes: list[str]) -> dict[str, Any]:
    accts = [{**_ACCOUNTS[i], "role": role} for i, role in roles.items()]
    contacts = {i: [c for c in _CONTACTS if c["account_id"] == i] for i in roles}
    return {"site": site_rec, "crm_accounts": accts, "contacts": contacts, "score": score, "rule": rule, "notes": notes, "about_text": None}


def score(total: float, **flags: Any) -> dict[str, Any]:
    return {
        "total": total,
        "components": {"street": 45, "zip": 10, "city": 10, "phone": 20, "name": 21.5},
        "street_match": True, "phone_match": True, "admin_match": False, "name_similarity": 86.0,
        "address_differs": False, **flags,
    }


def rename_grove_city() -> Proposal:
    return Proposal(
        kind=ProposalKind.RENAME, priority="low",
        title="Rename to Bellhaven Rehabilitation & Nursing of Grove City",
        summary="Cosmetic name variance (Rehab/Rehabilitation, and/&).",
        subject_account_id=GROVE_CITY, site_slug="bellhaven-rehabilitation-nursing-of-grove-city",
        actions=[Action(op="patch", account_id=GROVE_CITY, fields={"name": "Bellhaven Rehabilitation & Nursing of Grove City"}, label="Rename to the website name")],
        evidence=evidence(
            site("bellhaven-rehabilitation-nursing-of-grove-city", "Bellhaven Rehabilitation & Nursing of Grove City", "3985 Broadway", "Grove City", "43123", "(614) 555-7729", "Marie Vogel"),
            {GROVE_CITY: "subject"}, score(96.5), "Same street and phone under Bellhaven; only the name differs.", ["Name differs cosmetically."],
        ),
    )


def chow_tiffin() -> Proposal:
    return Proposal(
        kind=ProposalKind.CHOW_REPARENT, priority="high",
        title="Change of ownership: Bellhaven of Tiffin (from Cedar Trail)",
        summary="Account has revenue and open AR: create a new Bellhaven account and point the old one at it.",
        subject_account_id=TIFFIN_OLD, site_slug="bellhaven-of-tiffin",
        actions=[
            Action(
                op="create",
                fields={
                    "name": "Bellhaven of Tiffin", "parent_id": BELLHAVEN_PARENT_ID, "billing_street": "45 St Lawrence Dr",
                    "billing_city": "Tiffin", "billing_state": "OH", "billing_zip": "44883", "care_type": "Skilled Nursing",
                    "phone": "(260) 589-2589", "note": f"Change of ownership from {TIFFIN_OLD}",
                },
                label="Create Bellhaven of Tiffin under Bellhaven",
            ),
            Action(
                op="patch", account_id=TIFFIN_OLD,
                fields={"chow_current_account": NEW_ACCOUNT_REF, "note": f"CHOW: now operated by Bellhaven as {NEW_ACCOUNT_REF}"},
                label="Point the Cedar Trail account at the new account",
            ),
        ],
        evidence=evidence(
            site("bellhaven-of-tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "44883", "(260) 589-2589", "Walt Osei"),
            {TIFFIN_OLD: "old"}, score(80.0, phone_match=False), "Matched account is under Cedar Trail with revenue AND open AR (SOP).", ["Lifetime revenue 84000, outstanding AR 12400."],
        ),
    )


def duplicate_port_clinton() -> Proposal:
    return Proposal(
        kind=ProposalKind.DUPLICATE, priority="medium",
        title="Duplicate: Harborview Nursing & Rehab of Port Clinton",
        summary="Same address as Bellhaven of Port Clinton; retire the Harborview copy.",
        subject_account_id=PORT_CLINTON_HV, site_slug="bellhaven-of-port-clinton",
        actions=[Action(op="patch", account_id=PORT_CLINTON_HV, fields={"duplicate_of_account": PORT_CLINTON_BH, "status": "Inactive", "note": "Duplicate of Bellhaven of Port Clinton"}, label="Mark Harborview copy as duplicate")],
        evidence=evidence(
            site("bellhaven-of-port-clinton", "Bellhaven of Port Clinton", "1420 Harbor Point Dr", "Port Clinton", "43452", "(330) 733-6423", "Brenda Whitfield"),
            {PORT_CLINTON_BH: "survivor", PORT_CLINTON_HV: "loser"}, score(90.0, admin_match=True), "Two accounts share the street; survivor is under Bellhaven.", [],
        ),
    )


def confirmed_wooster() -> Proposal:
    return Proposal(
        kind=ProposalKind.CONFIRMED, priority="low", title="Bellhaven of Wooster", summary="Website and CRM agree.",
        subject_account_id=WOOSTER, site_slug="bellhaven-of-wooster", actions=[],
        evidence=evidence(
            site("bellhaven-of-wooster", "Bellhaven of Wooster", "2301 Cleveland Road", "Wooster", "44691", "(517) 712-7557", "Lorna Sorensen"),
            {WOOSTER: "subject"}, score(100.0), "Exact match.", [],
        ),
    )


def broken_patch() -> Proposal:
    return Proposal(
        kind=ProposalKind.NOT_ON_WEBSITE, priority="medium", title="Needs review: ghost account", summary="Not on the website.",
        subject_account_id="001GHOST0000000000",
        actions=[Action(op="patch", account_id="001GHOST0000000000", fields={"status": "Needs Review"}, label="Flag for review")],
        evidence={"site": None, "crm_accounts": [], "contacts": {}, "score": None, "rule": "CRM-only account", "notes": [], "about_text": None},
    )


@pytest.fixture
def env():
    store = Store(":memory:")
    crm = FakeCRM()
    settings = Settings(token="t", base_url="https://crm.example.test", reviewer_name="tester")
    proposals = {
        "rename": rename_grove_city(),
        "chow": chow_tiffin(),
        "dup": duplicate_port_clinton(),
        "confirmed": confirmed_wooster(),
        "broken": broken_patch(),
    }
    run_id = store.start_run()
    store.upsert_proposals(proposals.values(), run_id)
    client = TestClient(create_app(settings, store, crm))
    return client, store, crm, {k: p.fingerprint for k, p in proposals.items()}


def test_queue_lists_pending_without_confirmed(env):
    client, _, _, fps = env
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Bellhaven CRM Sync" in html
    assert "Rename to Bellhaven Rehabilitation &amp; Nursing of Grove City" in html
    assert "Change of ownership: Bellhaven of Tiffin (from Cedar Trail)" in html
    assert "Duplicate: Harborview Nursing &amp; Rehab of Port Clinton" in html
    assert f"/proposals/{fps['confirmed']}" not in html
    assert f"/proposals/{fps['chow']}/approve" in html
    # CRM browser deep link and website link
    assert "https://crm.example.test/crm/t/accounts/001U6RW32TY0WSXZZB" in html
    assert f"{SITE}/communities/bellhaven-of-tiffin" in html
    # high priority sorts first
    assert html.index("Bellhaven of Tiffin (from Cedar Trail)") < html.index("Grove City")


def test_confirmed_view(env):
    client, _, _, fps = env
    html = client.get("/?status=confirmed").text
    assert f"/proposals/{fps['confirmed']}" in html
    assert "Bellhaven of Wooster" in html
    assert f"/proposals/{fps['chow']}" not in html
    assert f"/proposals/{fps['confirmed']}/approve" not in html  # informational, no buttons


def test_kind_filter(env):
    client, _, _, fps = env
    html = client.get("/?status=pending&kind=rename").text
    assert f"/proposals/{fps['rename']}" in html
    assert f"/proposals/{fps['chow']}\"" not in html


def test_detail_shows_evidence_score_and_actions(env):
    client, _, _, fps = env
    r = client.get(f"/proposals/{fps['chow']}")
    assert r.status_code == 200
    html = r.text
    assert "Website says" in html
    assert "Walt Osei" in html  # website administrator
    assert "Bellhaven of Tiffin" in html  # site + CRM name
    assert "Cedar Trail Communities (Parent Account)" in html  # current CRM parent
    assert "$84,000" in html and "$12,400" in html  # SOP facts
    assert "SOP" in html
    assert "Match score" in html and ">80<" in html
    assert "Create Bellhaven of Tiffin under Bellhaven" in html
    assert "Point the Cedar Trail account at the new account" in html
    assert "the newly created account" in html
    assert NEW_ACCOUNT_REF not in html.split("<pre>")[0]  # placeholder rendered readably outside raw payloads
    assert "POST /api/v1/accounts" in html and f"PATCH /api/v1/accounts/{TIFFIN_OLD}" in html
    # parent_id is shown with its name, resolved from other proposals' evidence
    assert "(Bellhaven Senior Living (Parent Account))" in html
    assert "Approve &amp; apply" in html


def test_detail_highlights_mismatches(env):
    client, _, _, fps = env
    html = client.get(f"/proposals/{fps['rename']}").text
    assert "Bellhaven Rehab and Nursing of Grove City" in html  # CRM name
    assert 'class="cell diff' in html  # name differs
    assert 'class="cell same' in html  # street matches
    assert "<del>Bellhaven Rehab and Nursing of Grove City</del>" in html  # old -> new


def test_approve_applies_once(env):
    client, store, crm, fps = env
    fp = fps["chow"]
    r = client.post(f"/proposals/{fp}/approve", data={"note": "per SOP"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/proposals/{fp}")

    sp = store.get(fp)
    assert sp.status == "applied"
    assert sp.decided_by == "tester" and sp.reviewer_note == "per SOP"
    assert [w[0] for w in crm.writes] == ["create", "patch"]
    new_id = sp.apply_result["created_account_id"]
    assert crm.writes[1][1] == TIFFIN_OLD
    assert crm.writes[1][2]["chow_current_account"] == new_id
    assert crm.raw(new_id)["parent_id"] == BELLHAVEN_PARENT_ID

    html = client.get(r.headers["location"]).text
    assert "Approved and applied to the CRM." in html
    assert new_id in html
    assert f"https://crm.example.test/crm/t/accounts/{new_id}" in html

    again = client.post(f"/proposals/{fp}/approve", follow_redirects=False)
    assert again.status_code == 409
    assert len(crm.writes) == 2
    assert store.get(fp).status == "applied"
    # rejecting an applied proposal is also refused
    assert client.post(f"/proposals/{fp}/reject", follow_redirects=False).status_code == 409


def test_approve_confirmed_is_noop_write(env):
    client, store, crm, fps = env
    r = client.post(f"/proposals/{fps['confirmed']}/approve", follow_redirects=False)
    assert r.status_code == 303
    assert store.get(fps["confirmed"]).status == "applied"
    assert crm.writes == []


def test_reject_then_reopen(env):
    client, store, crm, fps = env
    fp = fps["rename"]
    r = client.post(f"/proposals/{fp}/reject", data={"note": "keep CRM name", "back": "pending"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?status=pending")
    assert store.get(fp).status == "rejected"
    assert store.get(fp).reviewer_note == "keep CRM name"
    assert crm.writes == []
    assert "Rejected" in client.get(r.headers["location"]).text
    assert f"/proposals/{fp}" in client.get("/?status=rejected").text

    assert client.post(f"/proposals/{fp}/approve", follow_redirects=False).status_code == 409
    assert crm.writes == []

    r = client.post(f"/proposals/{fp}/reopen", follow_redirects=False)
    assert r.status_code == 303
    assert store.get(fp).status == "pending"
    # reopening something that is pending is refused
    assert client.post(f"/proposals/{fp}/reopen", follow_redirects=False).status_code == 409


def test_failed_apply_is_recorded_and_reopenable(env):
    client, store, crm, fps = env
    fp = fps["broken"]
    r = client.post(f"/proposals/{fp}/approve", follow_redirects=False)
    assert r.status_code == 303
    sp = store.get(fp)
    assert sp.status == "failed"
    assert "404" in sp.apply_result["error"]
    html = client.get(r.headers["location"]).text
    assert "Apply failed" in html and "account not found" in html
    assert "Needs review: ghost account" in client.get("/?status=failed").text
    assert client.post(f"/proposals/{fp}/reopen", follow_redirects=False).status_code == 303
    assert store.get(fp).status == "pending"


def test_stale_can_be_approved(env):
    client, store, crm, fps = env
    store.upsert_proposals([duplicate_port_clinton()], store.start_run())  # everything else goes stale
    assert store.get(fps["rename"]).status == "stale"
    assert "Stale:" in client.get(f"/proposals/{fps['rename']}").text
    assert client.post(f"/proposals/{fps['rename']}/approve", follow_redirects=False).status_code == 303
    assert store.get(fps["rename"]).status == "applied"
    assert crm.writes == [("patch", GROVE_CITY, {"name": "Bellhaven Rehabilitation & Nursing of Grove City"})]


def test_cross_origin_post_refused(env):
    client, store, crm, fps = env
    r = client.post(f"/proposals/{fps['chow']}/approve", headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert crm.writes == [] and store.get(fps["chow"]).status == "pending"
    ok = client.post(f"/proposals/{fps['chow']}/approve", headers={"origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303


def test_unknown_proposal_404(env):
    client, *_ = env
    assert client.get("/proposals/nope").status_code == 404
    assert client.post("/proposals/nope/approve").status_code == 404
    assert client.get("/api/proposals/nope").status_code == 404


def test_api_and_health(env):
    client, store, crm, fps = env
    data = client.get("/api/proposals").json()
    assert len(data) == 5
    assert {d["kind"] for d in data} >= {"rename", "chow_reparent", "duplicate", "confirmed"}
    pending = client.get("/api/proposals?status=pending").json()
    assert len(pending) == 5

    h = client.get("/health").json()
    assert h["ok"] is True and h["pending"] == 4  # confirmed is informational, not actionable

    client.post(f"/proposals/{fps['chow']}/approve")
    one = client.get(f"/api/proposals/{fps['chow']}").json()
    assert one["status"] == "applied"
    assert one["apply_result"]["ok"] is True
    assert [s["op"] for s in one["apply_result"]["steps"]] == ["create", "patch"]
    assert [d["fingerprint"] for d in client.get("/api/proposals?status=applied").json()] == [fps["chow"]]
    assert client.get("/health").json()["pending"] == 3


def test_runs_and_static(env):
    client, store, *_ = env
    html = client.get("/runs").text
    assert "Pipeline runs" in html and "#1" in html
    css = client.get("/static/app.css")
    assert css.status_code == 200 and ".btn-approve" in css.text


def test_counts_in_nav(env):
    client, *_ = env
    html = client.get("/").text
    assert 'Pending<span class="count">4</span>' in html
    assert 'Confirmed (no change)<span class="count">1</span>' in html
