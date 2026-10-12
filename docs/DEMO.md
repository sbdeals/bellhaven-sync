# Demo script (about 2 minutes)

Two terminals. Terminal 1 runs the app, terminal 2 is for the pipeline. Have the CRM
browser open in another tab:
`https://analyst-assessment-production.up.railway.app/crm/<your token>/accounts`

## Part 1 — the day-one queue (snapshot mode, no live writes)

The live CRM copy is already corrected, so to show the full queue the pipeline runs
against the CRM snapshot captured on 2026-10-11 (`tests/fixtures/crm`). Approvals in
this mode write to `data/snapshot_state.json`, never to the live CRM.

```bash
uv run bellhaven-sync run --snapshot tests/fixtures/crm
uv run bellhaven-sync review --snapshot tests/fixtures/crm
```

1. Open http://127.0.0.1:8000. Point out the counts: 28 actionable, 18 confirmed.
2. Open **Bellhaven of Tiffin** (chow_reparent). Talk through: website says Tiffin, CRM
   has it under Cedar Trail with revenue 84,000 and AR 12,400, so the SOP applies. Show
   "What Approve will do": POST a new account under Bellhaven, then PATCH
   `chow_current_account` on the old one. Click **Approve**.
3. Open **Bellhaven at Union Square** (create). Show the "possibly related" Juniper Point
   account: same city and zip, but different street, phone and administrator, so it is
   not a match. Approve or reject as you see fit.
4. Open one **duplicate**, e.g. Harborview Shores of Erie. Show survivor rule: the
   Bellhaven copy wins because its parent is Bellhaven and the administrator matches a
   contact. Click **Reject** with a note to show rejection is recorded.
5. In terminal 2 run `uv run bellhaven-sync run --snapshot tests/fixtures/crm` again and
   point at the summary line: zero new proposals, the rejected one is "previously decided".

## Part 2 — a live change landing in the CRM

```bash
uv run bellhaven-sync run
uv run bellhaven-sync review
```

1. The live queue is short because the copy is already corrected. Open
   **Bellhaven of Sandusky** (chow_out): the facility is not on the website and
   "Millstone Care of Sandusky" sits at the same address under Millstone. Revenue and AR
   are non-zero, so the SOP says keep the account and set the CHOW pointer.
2. Click **Approve & apply**. The result card shows the PATCH response.
3. Click "Open in CRM" and show `chow_current_account` on Bellhaven of Sandusky in the
   CRM browser.
4. Open **Harborview Care Group (Parent Account)** (parent_acquired, low priority). It is
   a judgment call; reject it with a note, or approve it. Either is defensible.
5. Show the **Confirmed** tab: all 35 website locations matched, with the score breakdown
   on any one of them (Ashtabula is a good example: PO Box billing address, matched on
   phone and administrator).
