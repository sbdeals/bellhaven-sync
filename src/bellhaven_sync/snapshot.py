"""Snapshot / replay mode: run and review the day-one queue against captured data.

- `SnapshotCRM` implements the `crm.CRM` interface over `<dir>/accounts.json` and
  `<dir>/contacts.json` (the `{"data": [...]}` format of `tests/fixtures/crm/`). Every
  accepted write is persisted to a separate state file and re-applied over the snapshot on
  load, so approvals made in the review app survive restarts. The snapshot files themselves
  are never modified. Validation mirrors the live sandbox.
- `fixture_fetcher` serves the captured website HTML (`tests/fixtures/site/`) to the scraper.

Nothing in this module talks to the network.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .crm import CRMError
from .models import Account, Contact

log = logging.getLogger(__name__)

Fetcher = Callable[[str], str]

MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "name", "parent_id", "status", "note", "care_type", "phone", "billing_street", "billing_city",
        "billing_state", "billing_zip", "chow_current_account", "duplicate_of_account",
    }
)
STATUSES: frozenset[str] = frozenset({"Active", "Inactive", "Needs Review"})
SNAPSHOT_ID_PREFIX = "001SNAP"
_SNAPSHOT_ID_RE = re.compile(rf"^{SNAPSHOT_ID_PREFIX}(\d+)$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
STATE_VERSION = 1


def default_state_path(db_path: Path | str) -> Path:
    """Where snapshot writes persist by default: next to the SQLite store."""
    return Path(db_path).parent / "snapshot_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))  # FileNotFoundError if missing: fail loudly
    rows = data["data"] if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected {{'data': [...]}} or a JSON list")
    return rows


class SnapshotCRM:
    """File-backed CRM double with the same interface and validation as the sandbox API."""

    def __init__(self, snapshot_dir: Path | str, *, state_path: Path | str | None = None):
        self.snapshot_dir = Path(snapshot_dir)
        self.state_path = Path(state_path) if state_path else None  # None -> writes stay in memory
        self._lock = threading.RLock()
        self._accounts: dict[str, dict[str, Any]] = {
            r["account_id"]: dict(r) for r in _load_rows(self.snapshot_dir / "accounts.json")
        }
        self._contacts: list[dict[str, Any]] = [dict(r) for r in _load_rows(self.snapshot_dir / "contacts.json")]
        self._overrides: dict[str, dict[str, Any]] = {}  # rows changed/created since the snapshot
        self.writes: list[dict[str, Any]] = []
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._overrides = {aid: dict(row) for aid, row in state.get("accounts", {}).items()}
            self.writes = list(state.get("writes", []))
            self._accounts.update(copy.deepcopy(self._overrides))
            log.info("snapshot state %s: %d changed account(s), %d write(s) replayed",
                     self.state_path, len(self._overrides), len(self.writes))

    # ------------------------------------------------------------------ reads
    def list_accounts(self) -> list[Account]:
        with self._lock:
            return [Account.from_api(self._with_parent_name(a)) for a in self._accounts.values()]

    def get_account(self, account_id: str) -> Account:
        with self._lock:
            if account_id not in self._accounts:
                raise CRMError(404, "account not found", "GET", f"/accounts/{account_id}")
            return Account.from_api(self._with_parent_name(self._accounts[account_id]))

    def list_contacts(self, account_id: str | None = None) -> list[Contact]:
        with self._lock:
            rows = self._contacts if account_id is None else [c for c in self._contacts if c.get("account_id") == account_id]
            return [Contact.from_api(c) for c in rows]

    # ----------------------------------------------------------------- writes
    def create_account(self, fields: dict[str, Any]) -> Account:
        with self._lock:
            if not fields.get("name"):
                raise CRMError(422, "name is required", "POST", "/accounts")
            mutable = {k: v for k, v in fields.items() if k in MUTABLE_FIELDS}
            self._validate(mutable, "POST", "/accounts")
            new_id = self._next_id()
            row: dict[str, Any] = {
                "account_id": new_id, "name": "", "parent_id": "", "billing_street": "", "billing_city": "",
                "billing_state": "", "billing_zip": "", "care_type": "", "status": "Active", "phone": "",
                "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "",
                "note": "", "created_by_candidate": True, "updated_at": _now(),
            }
            row.update(mutable)
            self._accounts[new_id] = row
            self._record("create", new_id, mutable)
            return self.get_account(new_id)

    def update_account(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path = f"/accounts/{account_id}"
            if account_id not in self._accounts:
                raise CRMError(404, "account not found", "PATCH", path)
            mutable = {k: v for k, v in fields.items() if k in MUTABLE_FIELDS}
            if not mutable:
                raise CRMError(422, f"No mutable fields provided. Mutable: {sorted(MUTABLE_FIELDS)}", "PATCH", path)
            self._validate(mutable, "PATCH", path)
            self._accounts[account_id].update(mutable)
            self._accounts[account_id]["updated_at"] = _now()
            self._record("patch", account_id, mutable)
            return {"account_id": account_id, "message": "updated", "fields": sorted(mutable)}

    # ---------------------------------------------------------------- helpers
    def _validate(self, fields: dict[str, Any], method: str, path: str) -> None:
        if "status" in fields and fields["status"] not in STATUSES:
            raise CRMError(422, f"status must be one of {sorted(STATUSES)}", method, path)
        if fields.get("parent_id") and fields["parent_id"] not in self._accounts:
            raise CRMError(422, "parent_id does not exist", method, path)

    def _next_id(self) -> str:
        nums = [int(m.group(1)) for aid in self._accounts if (m := _SNAPSHOT_ID_RE.match(aid))]
        return f"{SNAPSHOT_ID_PREFIX}{(max(nums) + 1) if nums else 1:06d}"

    def _with_parent_name(self, a: dict[str, Any]) -> dict[str, Any]:
        a = dict(a)
        pid = a.get("parent_id") or ""
        a["parent_name"] = self._accounts[pid]["name"] if pid in self._accounts else ""
        return a

    def _record(self, op: str, account_id: str, fields: dict[str, Any]) -> None:
        self._overrides[account_id] = copy.deepcopy(self._accounts[account_id])
        self.writes.append({"op": op, "account_id": account_id, "fields": dict(fields), "at": _now()})
        self._persist()

    def _persist(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "version": STATE_VERSION,
            "snapshot_dir": str(self.snapshot_dir),
            "accounts": self._overrides,
            "writes": self.writes,
        }
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)


def fixture_fetcher(site_dir: Path | str) -> Fetcher:
    """A scraper `Fetcher` serving captured HTML: "/" -> index.html, "/about" -> about.html,
    "/communities[?page=N]" -> communities_pN.html (out-of-range pages return the last page,
    like the real site), "/communities/<slug>" -> communities/<slug>.html."""
    root = Path(site_dir)
    pages = {
        int(m.group(1)): p
        for p in root.glob("communities_p*.html")
        if (m := re.fullmatch(r"communities_p(\d+)\.html", p.name))
    }

    def fetch(path: str) -> str:
        parts = urlsplit(path)
        p = parts.path.rstrip("/") or "/"
        if p == "/":
            file = root / "index.html"
        elif p == "/about":
            file = root / "about.html"
        elif p == "/communities":
            if not pages:
                raise LookupError(f"no communities_p*.html pages in {root}")
            try:
                n = int(parse_qs(parts.query).get("page", ["1"])[0])
            except ValueError:
                n = 1
            file = pages.get(n) or pages[max(pages) if n > max(pages) else min(pages)]
        elif p.startswith("/communities/") and _SLUG_RE.match(slug := p[len("/communities/"):]):
            file = root / "communities" / f"{slug}.html"
        else:
            raise LookupError(f"no captured page for {path!r}")
        if not file.is_file():
            raise LookupError(f"no captured page for {path!r} ({file})")
        return file.read_text(encoding="utf-8")

    return fetch
