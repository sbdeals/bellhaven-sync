from __future__ import annotations

import json
import re
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



# ------------------------------------------------- duplicate clusters: choosing the survivor
MONROE_BH = "001U1750VLVJAGG1S5"  # Bellhaven Gardens of Monroe (under Bellhaven)
MONROE_CT = "0011AB44D05WLA9HTX"  # Cedar Trail of Monroe
MONROE_HV = "00159PL81N38KM4FHM"  # Monroe Gardens Care Center (Harborview)
MONROE_IDS = [MONROE_BH, MONROE_CT, MONROE_HV]


def candidate(acct_id: str, rank: int, reason: str, *, phone: bool = False, admin: bool = False, sim: float = 70.0) -> dict[str, Any]:
    contacts = [{k: c.get(k) for k in ("name", "title", "email", "phone", "is_active")} for c in _CONTACTS if c["account_id"] == acct_id]
    return {
        **_ACCOUNTS[acct_id], "contacts": contacts, "phone_match": phone, "admin_match": admin,
        "name_similarity": sim, "rank": rank, "rank_reason": reason,
    }


def retire_actions(survivor: str, ids: list[str]) -> list[Action]:
    return [
        Action(op="patch", account_id=i, fields={"duplicate_of_account": survivor, "status": "Inactive", "note": f"Duplicate of {survivor}"},
               label=f"Retire {_ACCOUNTS[i]['name']} as a duplicate")
        for i in ids
        if i != survivor
    ]


def duplicate_monroe(tie: bool = False) -> Proposal:
    """One proposal for a 3-account cluster, built by hand to the engine's evidence["cluster"] contract."""
    site_rec = site("bellhaven-gardens-of-monroe", "Bellhaven Gardens of Monroe", "750 Stewart Rd", "Monroe", "48162", "(814) 412-2218", "Phil Mabry", care=("Assisted Living",))
    site_rec["state"] = "MI"
    ev = evidence(site_rec, {MONROE_BH: "survivor", MONROE_CT: "loser", MONROE_HV: "loser"}, None, "Three CRM accounts share 750 Stewart Rd.", [])
    ev["cluster"] = {
        "candidates": [
            candidate(MONROE_BH, 1, "Tie on every criterion; lowest account id." if tie else "Under Bellhaven and closest name to the website.", sim=100.0),
            candidate(MONROE_CT, 2, "Tie on every criterion." if tie else "Not under Bellhaven.", sim=60.0),
            candidate(MONROE_HV, 3, "Tie on every criterion." if tie else "Not under Bellhaven; lowest name similarity.", sim=55.0),
        ],
        "recommended": MONROE_BH, "tie": tie, "bellhaven_parent_id": BELLHAVEN_PARENT_ID,
    }
    return Proposal(
        kind=ProposalKind.DUPLICATE, priority="medium",
        title="Duplicate accounts: Bellhaven Gardens of Monroe (3 CRM records)",
        summary="Three CRM accounts at 750 Stewart Rd are one facility.",
        subject_account_id=MONROE_BH, site_slug="bellhaven-gardens-of-monroe",
        actions=retire_actions(MONROE_BH, MONROE_IDS), evidence=ev,
    )


def make_client(*proposals: Proposal):
    store = Store(":memory:")
    crm = FakeCRM()
    store.upsert_proposals(proposals, store.start_run())
    settings = Settings(token="t", base_url="https://crm.example.test", reviewer_name="tester")
    return TestClient(create_app(settings, store, crm)), store, crm


@pytest.fixture
def rebuild(monkeypatch):
    """Stub for the engine's pure rebuild function (it may not exist yet); records its calls."""
    import bellhaven_sync.proposals as proposals_mod

    calls: list[tuple[dict, str]] = []

    def stub(evidence: dict, survivor_id: str) -> list[Action]:
        calls.append((evidence, survivor_id))
        return retire_actions(survivor_id, [c["account_id"] for c in evidence["cluster"]["candidates"]])

    monkeypatch.setattr(proposals_mod, "build_duplicate_group_actions", stub, raising=False)
    return calls


def radios(html: str) -> list[str]:
    return re.findall(r'<input type="radio" name="survivor"[^>]*>', html)


def test_duplicate_cluster_shows_survivor_choice():
    p = duplicate_monroe()
    client, *_ = make_client(p)
    html = client.get(f"/proposals/{p.fingerprint}").text
    rs = radios(html)
    assert len(rs) == 3
    assert [r for r in rs if " checked" in r] == [r for r in rs if f'value="{MONROE_BH}"' in r]
    assert "Choose the surviving account" in html
    assert "No automatic recommendation" not in html
    assert "Approve will mark 2 copies Inactive pointing at the survivor" in html
    assert "Not under Bellhaven; lowest name similarity." in html  # rank reason
    assert "Elaine Vogel" in html and "Cedar Trail Communities (Parent Account)" in html
    assert "Website vs CRM" in html  # the comparison table is kept
    # the confirm dialog names the survivor picked in the radio group (substituted by base.html JS)
    assert "Survivor (kept): {survivor}" in html
    assert "querySelector('input[name=\"survivor\"]:checked')" in html
    # the radios live inside the approve form
    form = html[html.index('<form method="post" class="decision-form">'):]
    form = form[: form.index("</form>")]
    assert len(radios(form)) == 3 and f"/proposals/{p.fingerprint}/approve" in form
    # queue row
    q = client.get("/").text
    assert "2 copies · recommended: Bellhaven Gardens of Monroe" in q
    assert "Survivor (kept): Bellhaven Gardens of Monroe (001U1750VLVJAGG1S5)" in q


def test_duplicate_cluster_tie_banner_defaults_to_recommended():
    p = duplicate_monroe(tie=True)
    client, *_ = make_client(p)
    html = client.get(f"/proposals/{p.fingerprint}").text
    assert "No automatic recommendation: the candidates tied on every criterion; pick the survivor" in html
    rs = radios(html)
    assert len(rs) == 3 and [r for r in rs if " checked" in r] == [r for r in rs if f'value="{MONROE_BH}"' in r]


def test_old_per_loser_duplicate_keeps_old_layout(env):
    client, store, crm, fps = env
    html = client.get(f"/proposals/{fps['dup']}").text
    assert not radios(html) and "Choose the surviving account" not in html
    assert "Harborview Nursing &amp; Rehab of Port Clinton" in html
    # a survivor cannot be honoured without a candidate list: refused, nothing written
    r = client.post(f"/proposals/{fps['dup']}/approve", data={"survivor": PORT_CLINTON_BH})
    assert r.status_code == 400 and crm.writes == [] and store.get(fps["dup"]).status == "pending"


def test_approve_default_survivor_applies_stored_actions(rebuild):
    p = duplicate_monroe()
    client, store, crm = make_client(p)
    r = client.post(f"/proposals/{p.fingerprint}/approve", data={"survivor": MONROE_BH, "note": "ok"}, follow_redirects=False)
    assert r.status_code == 303
    assert rebuild == []  # stored actions used as-is
    assert crm.writes == [(a.op, a.account_id, a.fields) for a in p.actions]
    sp = store.get(p.fingerprint)
    assert sp.status == "applied" and sp.reviewer_note == "ok"
    assert sp.apply_result["chosen_survivor"] == MONROE_BH and "applied_actions" not in sp.apply_result
    assert "Survivor kept" in client.get(f"/proposals/{p.fingerprint}").text


def test_approve_with_other_survivor_rebuilds_and_records_it(rebuild):
    p = duplicate_monroe()
    client, store, crm = make_client(p)
    r = client.post(f"/proposals/{p.fingerprint}/approve", data={"survivor": MONROE_CT, "note": "Cedar Trail is the billing record"}, follow_redirects=False)
    assert r.status_code == 303
    assert [s for _, s in rebuild] == [MONROE_CT] and rebuild[0][0]["cluster"]["recommended"] == MONROE_BH
    assert sorted(w[1] for w in crm.writes) == sorted([MONROE_BH, MONROE_HV])
    assert all(w[0] == "patch" and w[2]["duplicate_of_account"] == MONROE_CT for w in crm.writes)
    assert crm.raw(MONROE_BH)["duplicate_of_account"] == MONROE_CT
    sp = store.get(p.fingerprint)
    assert sp.status == "applied"
    assert sp.reviewer_note == f"survivor={MONROE_CT} (Cedar Trail of Monroe); Cedar Trail is the billing record"
    # the stored proposal is untouched (stable fingerprint); only the apply result records the deviation
    assert sp.proposal.fingerprint == p.fingerprint and sp.proposal.actions == p.actions
    api = client.get(f"/api/proposals/{p.fingerprint}").json()
    assert api["apply_result"]["chosen_survivor"] == MONROE_CT
    assert api["apply_result"]["recommended_survivor"] == MONROE_BH
    assert [a["account_id"] for a in api["apply_result"]["applied_actions"]] == [w[1] for w in crm.writes]
    html = client.get(r.headers["location"]).text
    assert "Approved and applied to the CRM." in html
    assert "Survivor kept" in html and "chosen by reviewer; recommended was Bellhaven Gardens of Monroe" in html
    kept = html[html.index("Survivor kept"):]
    assert MONROE_CT in kept[:400] and "Cedar Trail of Monroe" in kept[:400]
    assert "Actions applied for the reviewer-chosen survivor" in html
    assert "2/2 steps" in html


def test_approve_with_unknown_survivor_is_refused(rebuild):
    p = duplicate_monroe()
    client, store, crm = make_client(p)
    r = client.post(f"/proposals/{p.fingerprint}/approve", data={"survivor": "001NOTINCLUSTER000"}, follow_redirects=False)
    assert r.status_code == 400
    assert "not one of the candidate accounts" in r.text
    assert crm.writes == [] and rebuild == []
    sp = store.get(p.fingerprint)
    assert sp.status == "pending" and sp.apply_result is None and sp.decided_by is None


def test_field_drift_has_a_badge():
    kind = getattr(ProposalKind, "FIELD_DRIFT", None)
    if kind is None:
        pytest.skip("ProposalKind.FIELD_DRIFT not added by the engine yet")
    p = Proposal(
        kind=kind, priority="low", title="Phone drift: Bellhaven Rehabilitation & Nursing of Grove City",
        summary="CRM phone differs from the website.", subject_account_id=GROVE_CITY,
        site_slug="bellhaven-rehabilitation-nursing-of-grove-city",
        actions=[Action(op="patch", account_id=GROVE_CITY, fields={"phone": "(614) 555-7729"}, label="Update phone to the website value")],
        evidence=evidence(
            site("bellhaven-rehabilitation-nursing-of-grove-city", "Bellhaven Rehabilitation & Nursing of Grove City", "3985 Broadway", "Grove City", "43123", "(614) 555-7729", "Marie Vogel"),
            {GROVE_CITY: "subject"}, score(96.5), "Matched account; one field differs from the website.", [],
        ),
    )
    client, *_ = make_client(p, rename_grove_city())
    q = client.get("/").text
    assert '<span class="badge kind kind-field_drift">Field drift</span>' in q
    assert '<span class="dot kind-field_drift"></span>Field drift' in q  # kind filter chip
    assert '<span class="badge kind kind-field_drift">Field drift</span>' in client.get(f"/proposals/{p.fingerprint}").text
    assert ".kind-field_drift" in client.get("/static/app.css").text
