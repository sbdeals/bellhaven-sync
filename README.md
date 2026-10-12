# bellhaven-sync

Keep the CRM in step with the communities Bellhaven Senior Living actually operates.

## What this is

Bellhaven's public website is the source of truth for which communities it runs. The CRM has
drifted from it: communities joined through the Harborview Care Group (2025) and Cedar Trail
(2026) acquisitions still sit under their old parents, some facilities exist two or three
times, some carry old names, one was sold to another operator, and a few new ones are
missing. `bellhaven-sync` is a daily pipeline that scrapes the website, reconciles every
location against the CRM sandbox, and turns each difference into a proposal that carries its
evidence and the exact API calls it would make.

Nothing is written to the CRM automatically. The daily run is read-only. A reviewer opens a
local web app, reads the evidence, and approves or rejects each proposal; only an explicit
Approve calls the CRM API. Matching is deterministic (normalization plus rule-based scoring,
no LLM), and re-runs are idempotent: a proposal that has been decided is never re-opened.

## Quick start

Requires [uv](https://docs.astral.sh/uv/); uv installs Python 3.12 if needed.

```bash
uv sync
cp .env.example .env              # Windows: copy .env.example .env
# edit .env and set BELLHAVEN_CRM_TOKEN
uv run bellhaven-sync run         # scrape + match + store proposals (read-only against the CRM)
uv run bellhaven-sync review      # start the review app
```

Then open http://127.0.0.1:8000.

| Command | What it does |
|---|---|
| `bellhaven-sync run` | Scrape the website, load the CRM, match, store proposals. Never writes to the CRM. |
| `bellhaven-sync scrape` | Print the scraped locations as JSON. |
| `bellhaven-sync status` | Proposal counts by status and recent runs. |
| `bellhaven-sync review` | Local review app on 127.0.0.1:8000. The only path that writes to the CRM. |

**Snapshot / replay mode.** Every command also accepts `--snapshot DIR` to run against a
captured CRM (`DIR/accounts.json`, `DIR/contacts.json`) and the captured website in
`DIR/../site`. `tests/fixtures/crm` is the sandbox as it was on 2026-10-11, before any
correction, so this replays the full day-one queue. Approvals in this mode write only to
`data/snapshot_state.json`, never to the live CRM. See `docs/DEMO.md`.

```bash
uv run bellhaven-sync run --snapshot tests/fixtures/crm
uv run bellhaven-sync review --snapshot tests/fixtures/crm
```

Configuration comes from environment variables; a `.env` file at the project root is read
automatically and never overrides variables that are already set.

| Variable | Default | Purpose |
|---|---|---|
| `BELLHAVEN_CRM_TOKEN` | (required) | Bearer token for the CRM API |
| `BELLHAVEN_BASE_URL` | `https://analyst-assessment-production.up.railway.app` | Website and API (`/api/v1`) host |
| `BELLHAVEN_DB_PATH` | `data/bellhaven_sync.db` | SQLite state: runs, proposals, decisions |
| `BELLHAVEN_REVIEWER` | `$USERNAME`, else `reviewer` | Name recorded on approve/reject |

Also available: `BELLHAVEN_PARENT_ACCOUNT_ID` (otherwise the Bellhaven parent is found by
name), `BELLHAVEN_REVIEW_HOST`, `BELLHAVEN_REVIEW_PORT`.

## How it works

scrape -> match -> propose -> review -> apply

1. **Scrape** (`scraper.py`). Walks the `/communities` directory pages, harvests any
   `/communities/<slug>` link from the homepage, then parses each detail page: address,
   care offerings, administrator, phone. Pure functions over HTML; the fetcher is injected.
2. **Load the CRM** (`crm.py`). `CRMClient` reads all accounts and contacts with pagination
   and retries. It is the only module that talks to the API.
3. **Match** (`normalize.py`, `match.py`). Normalizes names, streets, phones and care types,
   picks candidate accounts, scores them, and returns a match result per website location,
   including duplicate clusters.
4. **Propose** (`proposals.py`). Turns match results plus CRM state into proposals,
   applying the change-of-ownership SOP. Each proposal has a kind, priority, title, summary,
   evidence, confidence, and an ordered list of `patch`/`create` actions.
5. **Store** (`store.py`, `pipeline.py`). `pipeline.run()` stores proposals in SQLite,
   upserting by fingerprint, and prints a summary. Up to here everything is read-only
   against the CRM.
6. **Review** (`app/`). FastAPI + Jinja2, local only. The queue is sorted by priority then
   kind and can be filtered by status and kind. The detail view shows the website record,
   every CRM account involved (with CRM-browser links), the score breakdown, the rule that
   fired, and the exact API calls Approve will make.
7. **Apply** (`apply.py`). On Approve, executes the actions in order, resolving
   `$new.account_id` references to the id returned by a preceding create, stores the API
   responses, and marks the proposal `applied` (or `failed` with the error). Reject stores
   the reviewer's note.

`config.py` holds settings, `models.py` the shared dataclasses, `cli.py` the commands.

### Proposal kinds

| Kind | When | What Approve does |
|---|---|---|
| `confirmed` | Website and CRM agree | Nothing; informational |
| `rename` | Same facility, different CRM name | PATCH `name` (low priority for cosmetic variance, high for a different name) |
| `reparent` | Matched account is not under Bellhaven and does not meet the SOP condition | PATCH `parent_id` (and `name` if it differs) |
| `chow_reparent` | Matched account has lifetime revenue AND outstanding AR | POST a new account under Bellhaven, then PATCH the old one's `chow_current_account` + note |
| `create` | No CRM account matches | POST a new account under Bellhaven |
| `duplicate` | Several CRM accounts for one location | PATCH each loser: `duplicate_of_account` = survivor, status Inactive (Needs Review if it has revenue and AR) |
| `chow_out` | Bellhaven account not on the website; another owner has an account at the same address | PATCH `chow_current_account` = that account + note; status and parent unchanged |
| `not_on_website` | Bellhaven account not on the website; nobody else at that address | PATCH status Needs Review + note |
| `parent_acquired` | Acquired parent whose children are all reconciled | PATCH status Inactive + note (low priority) |

## Matching approach

**Normalization.** Names are lowercased, `&` becomes `and`, `rehab` becomes
`rehabilitation`, `centre` becomes `center`, `health care` becomes `healthcare`; punctuation
and the stopwords `the`, `of`, `at` are dropped. Street suffixes and directions are reduced to
USPS short forms (Street -> st, Avenue -> ave, Northwest -> nw, ...). Phones keep the last ten
digits. Website care badges map to CRM care types: Short-Term Rehabilitation & Nursing ->
Skilled Nursing, Memory Support -> Memory Care, Assisted Living -> Assisted Living.

**Candidates.** Only accounts in the same state AND with the same zip or the same normalized
city are considered. Parent accounts (no billing address) and accounts already superseded
(`duplicate_of_account` or `chow_current_account` set) are excluded.

**Score (0 to 100).** Street match 45, zip 10, city 10, phone 20, website administrator is an
active contact on the account 15, name similarity up to 25, plus 10 when the CRM name contains
the website's locality word. A candidate is a match when the street matches (or phone and
administrator both match) and the score is at least 60. Several matches form a duplicate
cluster. Name similarity alone never matches when street, phone and administrator all differ.

**Duplicate survivor.** The first rule that separates the copies wins: parent is Bellhaven;
has lifetime revenue; phone matches the website; administrator matches a contact; higher name
similarity; more contacts; lowest account id.

**SOP (change of ownership).** When a matched account must move to Bellhaven and it has both
lifetime revenue and outstanding AR, its parent is not changed. A new account is created under
Bellhaven and the old one points to it through `chow_current_account`, so billing history
stays where it is. Without open AR the parent is changed directly. A duplicate copy with
revenue and AR goes to Needs Review rather than Inactive.

**Bellhaven accounts missing from the website.** If another operator's account shares the
normalized street and zip, the facility changed hands (`chow_out`); otherwise it is flagged
for review (`not_on_website`).

### The tricky cases

- **Findlay is only on the homepage.** The directory lists 34 communities; the homepage
  announces a 35th. The scraper harvests detail links from `/` as well. In the CRM, Findlay
  has no parent and no AR, so it is reparented directly.
- **Pagination trap.** Asking for page 4 silently returns page 3 again, so the scraper stops
  on the "Page N of N" counter, not on an empty page.
- **Carlisle vs New Carlisle.** Carlisle is in PA, New Carlisle in OH, so the New Carlisle
  account is never a candidate (their names would otherwise look near-identical to a token
  comparison). Carlisle becomes a `create`.
- **Amberly Manor in two states.** The website's Amberly Manor is in Hudson, OH; the CRM's is
  in Colorado Springs, CO under Juniper Point. Different state, so no candidate: `create`.
- **Union Square.** "Union Square Senior Living" under Juniper Point shares the city and zip
  but has a different street, phone and administrator. Different facility: `create`, with the
  Juniper Point account shown as possibly related evidence only.
- **PO Box billing address.** Ashtabula's CRM street is "PO Box 517", but the phone and the
  administrator contact match. It is `confirmed`; the PO Box is a legitimate billing address
  and the difference is recorded in the evidence without proposing an address change.
- **Three Kettering copies.** An orphan, a Harborview copy and a Cedar Trail copy, none under
  Bellhaven and none with revenue. The survivor ordering picks one, which is reparented and
  renamed; the other two become duplicates of it.
- **CHOW SOP.** Tiffin and Marietta sit under Cedar Trail with revenue and open AR, so they
  get `chow_reparent` (new Bellhaven account plus pointer) instead of a parent change. Lima
  (Harborview, no AR) and Zanesville (Cedar Trail, no revenue, also renamed) are reparented
  directly.
- **Sandusky sold to Millstone.** Sandusky is under Bellhaven in the CRM but gone from the
  website, and "Millstone Care of Sandusky" sits at the same address under Millstone. It gets
  `chow_out`: `chow_current_account` points at the Millstone account, with a note; status and
  parent are left alone. Alliance and Coldwater have no other owner and go to Needs Review.
- **Harborview acquired.** The About page says Harborview joined in 2025. Once its children
  are reconciled, the Harborview parent gets a low-priority `parent_acquired` proposal (status
  Inactive + note); the reviewer decides.

Other duplicates follow the same rules: Port Clinton and Erie (Bellhaven + Harborview copies),
Monroe (three copies), Owosso (two Bellhaven copies; the survivor is the one whose phone and
administrator match the website).

## Idempotency and safe re-runs

- **Fingerprints.** Each proposal's fingerprint is a sha256 of canonical JSON of its kind,
  subject account, website slug and actions, with every `note` field removed. Notes can carry
  dates; fingerprints must not, so the same correction always gets the same fingerprint.
- **Decided proposals are never re-opened.** If a run produces a fingerprint whose status is
  approved, applied, rejected or failed, it is only counted as `previously_decided`.
- **Pending proposals are refreshed** (evidence, last seen run) rather than duplicated.
- **Stale.** A pending proposal that the latest run no longer produces is marked `stale`; it
  becomes pending again if a later run produces it.
- **Convergence after apply.** Once an approved change is applied, the next run sees the
  corrected CRM and simply does not generate that proposal again.
- `bellhaven-sync run` never writes to the CRM, so it is always safe to re-run. A second run
  against an unchanged website and CRM creates zero new proposals.

## Scheduling

**GitHub Actions** (`.github/workflows/daily.yml`). Runs daily at 11:00 UTC (07:00 EDT) and on
manual dispatch: checkout, uv with Python 3.12, `uv sync --frozen`, restore the previous db,
`uv run bellhaven-sync run`, write `bellhaven-sync status` to the job summary, save the db,
upload the db and run log as an artifact (30 days). A concurrency group prevents overlapping
runs and the job times out after 15 minutes. Any scrape, CRM or config error makes the CLI
exit non-zero, which fails the job.

Setup: add the repository secret `BELLHAVEN_CRM_TOKEN`; optionally set the repository
variable `BELLHAVEN_BASE_URL` (defaults to the sandbox).

How state persists: the SQLite db holds every proposal and decision, so each run restores
the previous db before running and saves it afterwards. The workflow uses the Actions cache:
each run saves under a unique key with a constant prefix and restores the newest entry with
that prefix (cache entries are immutable, so a single fixed key would never update). A
warning is emitted if no previous db was found.

Limitation: reviewer decisions are written to whichever db the review app opens. To review a
scheduled run, download its artifact and run `bellhaven-sync review` with `BELLHAVEN_DB_PATH`
pointing at it. Approved changes converge regardless, because the next scheduled run sees the
corrected CRM. Rejections made against a downloaded copy do not flow back to the cache,
though, so the scheduled db would still list them as pending. For a real deployment keep a
single db that both sides use: commit it to a `state` branch, keep it in S3, or use the VM
setup below.

**Linux VM** (`ops/crontab`, `ops/run-daily.sh`). The crontab runs the wrapper at 11:00 (VM
clock, normally UTC). The wrapper uses `set -euo pipefail`, takes a `flock` so runs never
overlap, loads secrets from `/etc/bellhaven-sync.env`, applies a 15-minute timeout, logs start
and end timestamps, and exits with the CLI's status. The db stays on the VM's disk, so
scheduled runs and a review app started on the same machine (reached through
`ssh -L 8000:127.0.0.1:8000`) share one db.

## Testing

```bash
uv run pytest -q            # offline: captured fixtures + in-memory FakeCRM
uv run pytest -q -m live    # opt-in smoke test against the real sandbox
```

The fixtures in `tests/fixtures/` are real captures of the website and CRM taken 2026-10-11.
Tests cover normalization, scoring, each decision branch, the SOP branch, survivor ordering,
fingerprint stability (note changes do not change it), store idempotency (a second run
yields zero new proposals), apply ordering with `$new` references against `FakeCRM`, and the
scheduling files. Tests marked `live` are skipped unless `-m live` is given.

## Project layout

```
bellhaven-sync/
  .github/workflows/daily.yml   daily scheduled run (GitHub Actions)
  ops/
    crontab                     equivalent schedule for a Linux VM
    run-daily.sh                cron wrapper: flock, env file, timeout, timestamps
  src/bellhaven_sync/
    config.py                   env + constants
    models.py                   SiteLocation, Account, Contact, Action, Proposal, ProposalKind
    crm.py                      CRMClient: the only code that talks to the API
    scraper.py                  website -> list[SiteLocation]
    normalize.py                name / street / phone / care-type normalization
    match.py                    candidate blocking, scoring, duplicate clusters
    proposals.py                match results + CRM state -> Proposals (incl. SOP)
    store.py                    SQLite: runs, proposals, decisions; upsert by fingerprint
    pipeline.py                 run(): scrape -> load CRM -> match -> propose -> store
    apply.py                    execute an approved proposal's actions
    app/                        FastAPI review UI
    cli.py                      bellhaven-sync run | scrape | status | review
  tests/
    fixtures/                   real website HTML and CRM dumps (2026-10-11)
    fakes.py                    in-memory FakeCRM
  docs/
    crm-openapi.json
    superpowers/specs/2026-10-11-bellhaven-sync-design.md
  data/                         SQLite state db (gitignored)
```

## Decisions and trade-offs

- **Propose, then a human approves.** The CRM is graded on end state and carries billing
  data, so the pipeline never writes on its own. Cost: corrections wait for a reviewer.
- **Deterministic rules, no LLM.** Every decision can be explained by a score breakdown and a
  named rule, and the same input always gives the same proposals. Cost: a new kind of data
  problem needs a new rule.
- **Block before scoring.** Requiring the same state and the same zip or city keeps
  look-alike names in other places out (Amberly Manor, Carlisle). Cost: a CRM account whose
  city and zip are both wrong will not be found and shows up as a `create` for the reviewer.
- **Conservative SOP.** Accounts with revenue and open AR are never moved or deactivated;
  they get a new Bellhaven account and a pointer, or Needs Review.
- **Notes excluded from fingerprints.** Dated notes cannot create "new" proposals for the same
  correction.
- **SQLite plus a CI cache.** No infrastructure to run. Cost: the cache is not durable
  storage and the review app works on a copy; see Scheduling for the production options.

Results (against the CRM snapshot captured 2026-10-11, `tests/fixtures/crm`):

- Website locations scraped: 35 (34 in the directory plus Findlay from the homepage).
- CRM accounts / contacts read: 121 / 67.
- Proposals: 46. Confirmed 18, rename 7 (3 real renames, 4 cosmetic), reparent 4,
  chow_reparent 2, duplicate 7, create 4, chow_out 1, not_on_website 2, parent_acquired 1.
  28 are actionable; applying them all makes 30 API writes (6 creates, 24 patches), and the
  next run converges to 35 confirmed and nothing else.
- Live CRM copy: see "What happened to the live copy" in the writeup below.
- Tests: `uv run pytest -q` runs the whole suite offline in about a second; one live smoke
  test is opt-in with `-m live`.

## Writeup

**Matching approach.** The website is treated as the source of truth for which communities
Bellhaven runs. Each scraped location is normalised (names: `&`/and, Rehab/Rehabilitation,
Centre/Center, stopwords; streets: USPS abbreviations; phones: digits) and compared only
against CRM accounts in the same state with the same zip or city. Within that block, a
street match is worth most, then phone, then "the website's administrator is a contact on
this account", then fuzzy name similarity. A location matches when the address matches, or
when phone and administrator both match (Ashtabula bills to a PO Box). Several address
matches form a duplicate cluster; the survivor is chosen by a fixed ordering (already under
Bellhaven, has revenue, phone match, contact match, name similarity, contact count, lowest
id) and the losers get `duplicate_of_account` and Inactive. Name similarity alone never
matches across a different street when the phone and administrator also differ, which is
what keeps "Union Square Senior Living" (Juniper Point) from being hijacked, and the state
block keeps "Amberly Manor" (Colorado) and "New Carlisle" out of the picture. The
change-of-ownership SOP is a single predicate (`lifetime_revenue > 0 and outstanding_ar > 0`)
applied before any re-parent: Tiffin and Marietta get a new account plus a CHOW pointer,
Lima and Findlay are re-parented directly. Bellhaven accounts that are not on the website
either point at the new owner when one exists at the same address (Sandusky, now Millstone)
or go to Needs Review (Alliance, Coldwater). The Harborview parent, whose children are all
reconciled and which the About page names as acquired, is proposed Inactive at low priority
for a human to decide.

**What happened to the live copy.** The exploration, spec and all code were built against a
snapshot of the sandbox taken at the start. Before this pipeline's first live run, the same
CRM copy had already been corrected by a separate agent session working from the same
token (its notes are prefixed "[Bellhaven review ...]"). Its outcome agrees with this
pipeline's on every location, with three differences: it set Sandusky to Needs Review
instead of pointing it at Millstone, chose a different Kettering survivor (the orphaned
copy; this pipeline's tie-break would have picked the Harborview copy, which is why the
tie-break is documented in each proposal), and also fixed Portsmouth's zip and added the
second care offering to two accounts. This pipeline's first live run therefore produced 35
confirmed matches and two live proposals for the recorded review: the Sandusky CHOW
pointer (approving it writes `chow_current_account` to the CRM) and the Harborview
parent (reviewer's call). That is also
the convergence property the design relies on: once the CRM is right, a run proposes
nothing. The full day-one queue can be replayed at any time with `--snapshot`.

**How AI tools were used.** Claude Code acted as orchestrator: it explored the site and API
by hand, wrote the design spec, the data model, the SQLite store and the CRM client, then
dispatched four Opus subagents in parallel against that contract (scraper; matcher,
proposals, pipeline and CLI; review app and apply; schedule and docs), each working
test-first on disjoint files. The orchestrator verified every report by re-running the
tests and the live scrape itself, caught one real mistake of its own (an exploratory PATCH
that the API accepted because `care_type` is free text; reverted within the minute), and
diffed the live CRM against the snapshot to discover the parallel session's writes rather
than trusting the "everything confirmed" summary.

**What I would build next.** (1) Persist the state db somewhere durable (a `state` branch
or S3) so reviewer decisions made on a laptop flow back to the scheduled run. (2) A
"facility identity" table keyed on normalised address so the matcher can carry forward
survivor choices and CHOW chains across days instead of recomputing them. (3) Proposals
for field drift on confirmed matches (zip typos, second care offerings, phone) behind a
separate low-priority tab, since the pipeline already has the evidence. (4) Alerting when
the scrape count drops, which is the one failure that could silently turn every account
into "not on website"; today the run refuses to proceed on an empty scrape but does not
page anyone.
