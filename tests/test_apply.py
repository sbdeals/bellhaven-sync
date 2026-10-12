from __future__ import annotations

import json

from bellhaven_sync.apply import ApplyResult, apply_proposal
from bellhaven_sync.models import NEW_ACCOUNT_REF, Action, Proposal, ProposalKind
from tests.conftest import BELLHAVEN_PARENT_ID

TIFFIN_OLD = "001U6RW32TY0WSXZZB"


def chow_tiffin() -> Proposal:
    return Proposal(
        kind=ProposalKind.CHOW_REPARENT,
        priority="high",
        title="Change of ownership: Bellhaven of Tiffin",
        summary="Create a new Bellhaven account; keep the Cedar Trail account for billing history.",
        subject_account_id=TIFFIN_OLD,
        site_slug="bellhaven-of-tiffin",
        actions=[
            Action(
                op="create",
                fields={
                    "name": "Bellhaven of Tiffin",
                    "parent_id": BELLHAVEN_PARENT_ID,
                    "billing_street": "45 St Lawrence Dr",
                    "billing_city": "Tiffin",
                    "billing_state": "OH",
                    "billing_zip": "44883",
                    "care_type": "Skilled Nursing",
                    "phone": "(260) 589-2589",
                    "note": f"Created by CHOW from {TIFFIN_OLD}",
                },
                label="Create Bellhaven of Tiffin under Bellhaven",
            ),
            Action(
                op="patch",
                account_id=TIFFIN_OLD,
                fields={
                    "chow_current_account": NEW_ACCOUNT_REF,
                    "note": f"CHOW: superseded by {NEW_ACCOUNT_REF}",
                },
                label="Point old Cedar Trail account at the new one",
            ),
        ],
    )


def test_chow_creates_then_patches_with_new_id(fake_crm):
    result = apply_proposal(chow_tiffin(), fake_crm)

    assert result.ok, result.error
    assert [w[0] for w in fake_crm.writes] == ["create", "patch"]
    new_id = result.created_account_id
    assert new_id and new_id.startswith("001NEW")

    created = fake_crm.raw(new_id)
    assert created["parent_id"] == BELLHAVEN_PARENT_ID
    assert created["name"] == "Bellhaven of Tiffin"

    op, patched_id, fields = fake_crm.writes[1]
    assert patched_id == TIFFIN_OLD
    assert fields["chow_current_account"] == new_id
    assert fields["note"] == f"CHOW: superseded by {new_id}"  # placeholder replaced inside strings
    old = fake_crm.raw(TIFFIN_OLD)
    assert old["chow_current_account"] == new_id
    assert old["parent_id"] == "001FWSQ30SFW6S7604"  # SOP: old account parent untouched

    assert [s.op for s in result.steps] == ["create", "patch"]
    assert all(s.ok for s in result.steps)
    assert result.steps[0].account_id == new_id
    assert result.steps[0].response["account_id"] == new_id
    assert result.steps[1].response["account_id"] == TIFFIN_OLD


def test_failure_stops_and_keeps_completed_steps(fake_crm):
    p = Proposal(
        kind=ProposalKind.RENAME,
        priority="low",
        title="bad",
        summary="",
        subject_account_id="001DOESNOTEXIST0000",
        actions=[
            Action(op="patch", account_id="001TZCSTBZFM6K5BGN", fields={"name": "Bellhaven Rehabilitation & Nursing of Grove City"}),
            Action(op="patch", account_id="001DOESNOTEXIST0000", fields={"name": "x"}),
            Action(op="patch", account_id="001TZCSTBZFM6K5BGN", fields={"status": "Inactive"}),
        ],
    )
    result = apply_proposal(p, fake_crm)

    assert result.ok is False
    assert "404" in result.error and "not found" in result.error
    assert [s.ok for s in result.steps] == [True, False]  # third action never attempted
    assert result.steps[1].error == result.error
    assert result.steps[1].response is None
    assert len(fake_crm.writes) == 1
    assert fake_crm.raw("001TZCSTBZFM6K5BGN")["status"] == "Active"


def test_single_bad_patch_has_no_writes(fake_crm):
    p = Proposal(
        kind=ProposalKind.RENAME, priority="low", title="t", summary="",
        actions=[Action(op="patch", account_id="001NOPE", fields={"name": "x"})],
    )
    result = apply_proposal(p, fake_crm)
    assert not result.ok
    assert len(result.steps) == 1 and result.steps[0].error
    assert fake_crm.writes == []


def test_unresolved_reference_is_never_sent(fake_crm):
    p = Proposal(
        kind=ProposalKind.CHOW_REPARENT, priority="high", title="t", summary="",
        actions=[Action(op="patch", account_id=TIFFIN_OLD, fields={"chow_current_account": NEW_ACCOUNT_REF})],
    )
    result = apply_proposal(p, fake_crm)
    assert not result.ok
    assert NEW_ACCOUNT_REF in result.error
    assert fake_crm.writes == []


def test_crm_validation_error_is_captured(fake_crm):
    p = Proposal(
        kind=ProposalKind.NOT_ON_WEBSITE, priority="medium", title="t", summary="",
        actions=[Action(op="patch", account_id=TIFFIN_OLD, fields={"status": "Closed"})],
    )
    result = apply_proposal(p, fake_crm)
    assert not result.ok and "422" in result.error


def test_confirmed_proposal_is_ok_with_no_steps(fake_crm):
    p = Proposal(kind=ProposalKind.CONFIRMED, priority="low", title="Bellhaven of Wooster", summary="matches", actions=[])
    result = apply_proposal(p, fake_crm)
    assert result.ok
    assert result.steps == []
    assert result.created_account_id is None
    assert fake_crm.writes == []


def test_to_dict_round_trips_through_json(fake_crm):
    result = apply_proposal(chow_tiffin(), fake_crm)
    d = result.to_dict()
    back = json.loads(json.dumps(d))
    assert back == d
    assert back["ok"] is True
    assert back["created_account_id"] == result.created_account_id
    assert [s["op"] for s in back["steps"]] == ["create", "patch"]
    assert set(back["steps"][0]) >= {"op", "account_id", "fields", "response", "ok", "error"}

    failed = ApplyResult(ok=False, steps=[], created_account_id=None, error="boom").to_dict()
    assert json.loads(json.dumps(failed)) == failed
