"""Tests for the orchestrator-owned contract: models (fingerprints) and the SQLite store."""

from __future__ import annotations

import json

from bellhaven_sync.models import NEW_ACCOUNT_REF, Account, Action, Proposal, ProposalKind, compute_fingerprint
from bellhaven_sync.store import Store


def _rename(note: str = "n", name: str = "Bellhaven of Chesterton") -> Proposal:
    return Proposal(
        kind=ProposalKind.RENAME,
        priority="high",
        title="Rename",
        summary="s",
        subject_account_id="0013NUZQHQUEZ8DXEG",
        site_slug="bellhaven-of-chesterton",
        actions=[Action(op="patch", account_id="0013NUZQHQUEZ8DXEG", fields={"name": name, "note": note}, label="rename")],
        evidence={"rule": "x"},
    )


def _chow() -> Proposal:
    return Proposal(
        kind=ProposalKind.CHOW_REPARENT,
        priority="high",
        title="CHOW Tiffin",
        summary="s",
        subject_account_id="001U6RW32TY0WSXZZB",
        site_slug="bellhaven-of-tiffin",
        actions=[
            Action(op="create", fields={"name": "Bellhaven of Tiffin", "parent_id": "0015QAPLGS3FVYEEEM"}, label="create"),
            Action(op="patch", account_id="001U6RW32TY0WSXZZB", fields={"chow_current_account": NEW_ACCOUNT_REF}, label="chow"),
        ],
    )


# ---------------------------------------------------------------- models


def test_fingerprint_ignores_notes_and_evidence():
    a, b = _rename(note="[2026-10-11] x"), _rename(note="[2026-10-12] y")
    b.evidence = {"rule": "different"}
    assert a.fingerprint == b.fingerprint


def test_fingerprint_changes_with_payload():
    assert _rename(name="A").fingerprint != _rename(name="B").fingerprint
    assert _rename().fingerprint != _chow().fingerprint


def test_fingerprint_is_deterministic_across_processes():
    p = _rename()
    assert compute_fingerprint(p) == compute_fingerprint(Proposal.from_dict(json.loads(json.dumps(p.to_dict()))))


def test_proposal_round_trip():
    p = _chow()
    q = Proposal.from_dict(json.loads(json.dumps(p.to_dict())))
    assert q.kind is ProposalKind.CHOW_REPARENT
    assert q.actions[1].fields["chow_current_account"] == NEW_ACCOUNT_REF
    assert q.fingerprint == p.fingerprint


def test_account_helpers():
    parent = Account.from_api({"account_id": "p", "name": "X (Parent Account)", "billing_street": "", "billing_city": ""})
    assert parent.is_parent_account
    a = Account.from_api({"account_id": "a", "name": "A", "lifetime_revenue": 10, "outstanding_ar": 1, "extra_unknown": 1})
    assert a.has_revenue_and_ar and not a.is_superseded
    assert Account.from_api({"account_id": "b", "name": "B", "chow_current_account": "zzz"}).is_superseded
    assert not Account.from_api({"account_id": "c", "name": "C", "lifetime_revenue": 10, "outstanding_ar": 0}).has_revenue_and_ar


# ----------------------------------------------------------------- store


def test_second_run_does_not_reopen_decided():
    st = Store(":memory:")
    r1 = st.start_run()
    s1 = st.upsert_proposals([_rename(), _chow()], r1)
    assert (s1.new, s1.refreshed, s1.previously_decided, s1.stale) == (2, 0, 0, 0)
    st.decide(_rename().fingerprint, "rejected", by="elias", note="keep legacy name")
    st.decide(_chow().fingerprint, "approved", by="elias")
    st.mark_applied(_chow().fingerprint, {"ok": True})

    r2 = st.start_run()
    s2 = st.upsert_proposals([_rename(note="new day"), _chow()], r2)
    assert (s2.new, s2.refreshed, s2.previously_decided, s2.stale) == (0, 0, 2, 0)
    assert st.get(_rename().fingerprint).status == "rejected"
    assert st.get(_chow().fingerprint).status == "applied"
    assert st.get(_chow().fingerprint).apply_result == {"ok": True}


def test_pending_refreshed_and_missing_marked_stale():
    st = Store(":memory:")
    r1 = st.start_run()
    st.upsert_proposals([_rename(), _chow()], r1)
    r2 = st.start_run()
    s2 = st.upsert_proposals([_rename(note="refreshed")], r2)
    assert (s2.new, s2.refreshed, s2.previously_decided, s2.stale) == (0, 1, 0, 1)
    assert st.get(_chow().fingerprint).status == "stale"
    assert st.get(_rename().fingerprint).proposal.actions[0].fields["note"] == "refreshed"
    # a stale proposal that comes back is reopened as pending
    r3 = st.start_run()
    s3 = st.upsert_proposals([_rename(), _chow()], r3)
    assert s3.refreshed == 2 and st.get(_chow().fingerprint).status == "pending"


def test_list_sorts_by_priority_and_filters():
    st = Store(":memory:")
    low = Proposal(kind=ProposalKind.CONFIRMED, priority="low", title="A", summary="", actions=[], site_slug="a")
    high = _rename()
    r = st.start_run()
    st.upsert_proposals([low, high], r)
    assert [s.proposal.title for s in st.list()] == ["Rename", "A"]
    assert [s.proposal.title for s in st.list(kind="confirmed")] == ["A"]
    st.decide(high.fingerprint, "rejected", by="x")
    assert [s.proposal.title for s in st.list(status="pending")] == ["A"]
    assert st.counts() == {"pending": 1, "rejected": 1}
    st.reopen(high.fingerprint)
    assert st.get(high.fingerprint).status == "pending"


def test_runs_recorded(tmp_path):
    st = Store(tmp_path / "x.db")
    r = st.start_run()
    summ = st.upsert_proposals([_rename()], r)
    st.finish_run(r, site_count=35, crm_count=121, summary=summ, notes="ok")
    runs = st.runs()
    assert runs[0]["site_count"] == 35 and runs[0]["proposals_new"] == 1 and runs[0]["finished_at"]
    st.close()
    assert Store(tmp_path / "x.db").get(_rename().fingerprint) is not None
