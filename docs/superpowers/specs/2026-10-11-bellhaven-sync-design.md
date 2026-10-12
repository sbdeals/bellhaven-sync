# Bellhaven CRM Sync — Design

Date: 2026-10-11. Author: Elias Rabayev (with Claude as orchestrator + Opus subagents).

## Goal

A daily pipeline that scrapes Bellhaven Senior Living's public website, reconciles every
location against the CRM sandbox, proposes corrections with evidence, and applies only
reviewer-approved changes through the CRM API. Re-runs never re-propose decided items.

## Inputs discovered during exploration

Website `https://analyst-assessment-production.up.railway.app`
- `/communities?page=N` — 3 pages, 16/16/2 cards; **34 listed**. Requesting page 4 silently
  returns page 3 again, so pagination must stop on the "Page N of N" counter, not on an empty page.
- `/` — homepage announces a 35th community (`/communities/bellhaven-meadows-of-findlay`) that
  is NOT in the directory. The scraper must also harvest `/communities/<slug>` links from `/`.
- `/about` — "In 2025 we welcomed the Harborview Care Group family of communities, and in 2026
  we expanded further with select communities joining us from Cedar Trail."
- Detail page `<dl class="detail">`: Address (street `<br>` "City, ST 12345"), Care Offerings
  (1..n badges), Administrator, Phone.
- Care badges: `Assisted Living`, `Memory Support`, `Short-Term Rehabilitation & Nursing`.

CRM API `.../api/v1` (Bearer token). 121 accounts, 67 contacts. `page_size` caps at 200.
- Account fields: account_id, name, parent_id, parent_name, billing_street/city/state/zip,
  care_type, status, phone, lifetime_revenue, outstanding_ar, chow_current_account,
  duplicate_of_account, note, created_by_candidate, updated_at.
- PATCH mutable fields: name, parent_id, status, note, care_type, phone, billing_street,
  billing_city, billing_state, billing_zip, chow_current_account, duplicate_of_account.
- `status` enum enforced: Active | Inactive | Needs Review. `parent_id` must exist.
  `care_type` is free text (CRM uses Skilled Nursing / Assisted Living / Memory Care /
  Independent Living). POST requires `name`.
- CRM browser: `/crm/<token>/accounts/<account_id>`.

Bellhaven parent account: `0015QAPLGS3FVYEEEM` "Bellhaven Senior Living (Parent Account)".
Other parents: Harborview Care Group `001FJZYHR7MLFMNPLL`, Cedar Trail Communities
`001FWSQ30SFW6S7604`, Millstone Health Partners, Juniper Point, Stonebridge.

## Known data situations (the matcher must handle all of these generically)

| Website location | CRM state | Expected outcome |
|---|---|---|
| 23 plain Bellhaven locations | Under Bellhaven, same address | **confirmed** (no-op, shown as informational) |
| Grove City, Sycamore Ridge, Arbors Dayton, Ashland | Under Bellhaven, cosmetic name variance (Rehab/Rehabilitation, &/and, Centre/Center, of/at, "The") | **rename, low priority** |
| Chagrin Falls / Willow Creek / Chesterton | Under Bellhaven as "Riverbend Manor Care Center" / "Sunny Acres Retirement Home" / "Chesterton Senior Commons" (same address, admin matches contact) | **rename, high priority** |
| Ashtabula | Under Bellhaven; CRM street is "PO Box 517" but phone + administrator contact match | **confirmed** (billing PO Box is legitimate; address difference recorded in evidence, no change) |
| Findlay (homepage only) | Exists, parent empty, revenue 22000, AR 0 | **reparent** directly to Bellhaven |
| Lima | Under Harborview, revenue 47000, AR 0 | **reparent** directly |
| Tiffin, Marietta | Under Cedar Trail, revenue > 0 AND AR > 0 | **chow_reparent**: create new account under Bellhaven, set `chow_current_account` on old, leave old otherwise untouched (SOP) |
| Zanesville | "Cedar Trail of Zanesville" under Cedar Trail, no revenue, same address, admin matches | **reparent + rename** in one proposal |
| Port Clinton, Erie | Bellhaven copy + Harborview copy at same address | **duplicate**: Harborview copy -> `duplicate_of_account`=Bellhaven copy, status Inactive |
| Monroe | Bellhaven copy + Harborview copy + Cedar Trail copy, same address | **duplicate** x2 |
| Owosso | Two Bellhaven copies, same address; website phone + administrator match one of them | **duplicate**: survivor is the phone/contact match |
| Kettering | Three copies (orphan, Harborview, Cedar Trail), none under Bellhaven, no revenue | survivor chosen by deterministic rules -> **reparent + rename** survivor, **duplicate** the other two |
| Union Square | "Union Square Senior Living" under Juniper Point: same city/zip, but different street, phone AND administrator | **create** new account; the Juniper Point account is listed as "possibly related" evidence only |
| Amberly Manor (Hudson OH) | "Amberly Manor" exists in Colorado Springs CO under Juniper Point | **create** (name collision, different state) |
| Batavia, Carlisle | No account (Carlisle must NOT fuzzy-match "New Carlisle") | **create** |
| Sandusky (CRM only) | Under Bellhaven, revenue 130000, AR 5200; "Millstone Care of Sandusky" at same address under Millstone | **chow_out**: set `chow_current_account` -> Millstone account, add note, leave status/parent (SOP) |
| Alliance, Coldwater (CRM only) | Under Bellhaven, not on website, no revenue, no other owner at address | **not_on_website**: status -> Needs Review + note |
| Harborview parent | All children reconciled; About page says acquired 2025 | **parent_acquired**: status -> Inactive + note (low priority; reviewer decides) |

## Architecture

```
src/bellhaven_sync/
  config.py      env + constants (token, base URL, db path, parent discovery)
  models.py      SiteLocation, Account, Contact, Action, Proposal, ProposalKind
  crm.py         CRMClient (httpx, pagination, retries) — the only thing that talks to the API
  scraper.py     scrape_site(fetch) -> list[SiteLocation]; pure functions over HTML
  normalize.py   name/street/phone/care-type normalization
  match.py       candidate blocking + scoring -> MatchResult per location; duplicate clusters
  proposals.py   turn match results + CRM state into Proposals (incl. SOP branch)
  store.py       SQLite store: runs + proposals, idempotent upsert by fingerprint
  pipeline.py    run(): scrape -> load CRM -> match -> propose -> store; prints summary
  apply.py       execute an approved Proposal's actions via CRMClient, resolve $refs
  app/           FastAPI review UI (list, detail w/ evidence, approve/reject, applied log)
  cli.py         bellhaven-sync run | scrape | review | status
tests/           pytest; fixtures are the real HTML + CRM dumps captured 2026-10-11
.github/workflows/daily.yml + ops/crontab
```

Data flow: `pipeline.run()` is read-only against the CRM. Only `apply.apply_proposal()`
writes, and only the review app calls it, only on an explicit Approve.

## Matching (deterministic, no LLM)

Normalization: lowercase; `&`->`and`; `rehab`->`rehabilitation`; `centre`->`center`;
`health care`->`healthcare`; drop punctuation; drop stopwords `the`, `of`, `at`, `-`.
Streets: USPS suffix/direction abbreviation to short form (Street->st, Avenue->ave,
Road->rd, Drive->dr, Lane->ln, Boulevard->blvd, Pike->pk, West->w, Northwest->nw, ...).
Phones: digits only, last 10. Care type map: Short-Term Rehabilitation & Nursing ->
Skilled Nursing; Memory Support -> Memory Care; Assisted Living -> Assisted Living.

Candidates: accounts with the same state AND (same zip OR same normalized city); exclude
parent accounts (empty billing address) and superseded accounts (non-empty
`duplicate_of_account` or `chow_current_account`).

Score (0..100): street exact-normalized 45; zip 10; city 10; phone 20; administrator
matches an active contact name on the account 15; name token similarity (rapidfuzz
token_set_ratio of normalized names) scaled to 0..25; +10 if CRM name contains the
website locality token (e.g. "chesterton"). Capped at 100.

Decision:
- `street_match` (or phone+admin match) with score >= 60 -> MATCH. Several such candidates
  -> DUPLICATE CLUSTER.
- No street/phone/admin match but name similarity >= 90 and same city -> MATCH with
  `address_differs` flag (Ashtabula PO Box).
- Otherwise -> NO MATCH (create). Name-only similarity never matches across different
  streets when phone and administrator also differ (Union Square).

Duplicate survivor ordering (first wins): parent is Bellhaven; lifetime_revenue > 0;
website phone matches; website administrator matches a contact; higher name similarity;
more contacts; lower account_id (deterministic tiebreak).

CRM-only Bellhaven children: for each Active account under Bellhaven with no website
match: if another non-Bellhaven, non-parent account shares the normalized street+zip ->
`chow_out`; else `not_on_website`.

## SOP (change of ownership)

When a matched account must move to Bellhaven: if `lifetime_revenue > 0 AND
outstanding_ar > 0` -> do NOT change parent. Create a new account (website name, address,
mapped care_type, phone, parent=Bellhaven, note referencing the old id), then PATCH old
account `chow_current_account = <new id>` plus a note. Otherwise PATCH `parent_id` directly.
A duplicate loser with revenue AND AR is set to Needs Review (not Inactive) with the
`duplicate_of_account` pointer, so billing history is never hidden.

## Proposals and idempotency

A Proposal = kind, priority, title, summary, subject, site_slug, `actions` (ordered list of
`patch`/`create` operations with `$new.account_id` refs), `evidence`, `confidence`, and a
`fingerprint` = sha256 of canonical JSON of (kind, subject_account_id, site_slug, actions
with every `note` field removed). Notes may carry dates; fingerprints must not.

Store rules: upsert by fingerprint. Existing status in {approved, applied, rejected, failed}
-> never re-opened (counted as `previously_decided`). Pending -> evidence/last_seen refreshed.
Pending proposals absent from the latest run -> `stale`. After an approved change is
applied, the next run sees the corrected CRM and simply does not generate it again.

## Review app

FastAPI + Jinja2, local only (`bellhaven-sync review` -> http://127.0.0.1:8000).
Queue sorted by priority then kind; filters by status/kind. Detail view shows the website
record, each CRM account involved (with CRM-browser deep links), the score breakdown and
the rule that fired, and the exact API calls that Approve will make. Approve executes the
actions in order, stores the API responses, marks `applied` (or `failed` with the error).
Reject stores the reviewer note. Nothing writes without Approve.

## Scheduling

`.github/workflows/daily.yml`: cron `0 11 * * *` (UTC) runs `bellhaven-sync run`,
restores/uploads the SQLite db as an artifact so decisions persist between runs, and fails
loudly on scrape errors. `ops/crontab` has the equivalent for a VM. The app is run on
demand by the reviewer.

## Testing

pytest with the captured fixtures. Unit tests for normalization, scoring, each decision
branch above, SOP branch, survivor ordering, fingerprint stability (note changes don't
change it), store idempotency (second run yields zero new proposals), and apply ordering
with `$new` refs against an in-memory FakeCRM. One opt-in live smoke test (`-m live`).
