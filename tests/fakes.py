"""In-memory CRM double implementing the same interface as `crm.CRMClient`.

Loaded from the captured fixtures so tests see the real 2026-10-11 sandbox state.
Writes are recorded in `.writes` so tests can assert exactly what would hit the API.
"""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from typing import Any

from bellhaven_sync.crm import CRMError
from bellhaven_sync.models import Account, Contact

FIXTURES = Path(__file__).parent / "fixtures"
MUTABLE = {
    "name", "parent_id", "status", "note", "care_type", "phone", "billing_street",
    "billing_city", "billing_state", "billing_zip", "chow_current_account", "duplicate_of_account",
}
STATUSES = {"Active", "Inactive", "Needs Review"}


class FakeCRM:
    def __init__(self, accounts: list[dict[str, Any]] | None = None, contacts: list[dict[str, Any]] | None = None):
        if accounts is None:
            accounts = json.loads((FIXTURES / "crm" / "accounts.json").read_text(encoding="utf-8"))["data"]
        if contacts is None:
            contacts = json.loads((FIXTURES / "crm" / "contacts.json").read_text(encoding="utf-8"))["data"]
        self._accounts: dict[str, dict[str, Any]] = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self._contacts: list[dict[str, Any]] = copy.deepcopy(contacts)
        self.writes: list[tuple[str, str | None, dict[str, Any]]] = []  # (op, account_id, fields)
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ reads
    def list_accounts(self) -> list[Account]:
        return [Account.from_api(self._with_parent_name(a)) for a in self._accounts.values()]

    def get_account(self, account_id: str) -> Account:
        if account_id not in self._accounts:
            raise CRMError(404, "account not found", "GET", f"/accounts/{account_id}")
        return Account.from_api(self._with_parent_name(self._accounts[account_id]))

    def list_contacts(self, account_id: str | None = None) -> list[Contact]:
        rows = self._contacts if account_id is None else [c for c in self._contacts if c["account_id"] == account_id]
        return [Contact.from_api(c) for c in rows]

    # ----------------------------------------------------------------- writes
    def create_account(self, fields: dict[str, Any]) -> Account:
        if not fields.get("name"):
            raise CRMError(422, "name is required", "POST", "/accounts")
        self._validate(fields)
        new_id = f"001NEW{next(self._ids):012d}"
        row = {
            "account_id": new_id, "name": "", "parent_id": "", "billing_street": "", "billing_city": "",
            "billing_state": "", "billing_zip": "", "care_type": "", "status": "Active", "phone": "",
            "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "",
            "note": "", "created_by_candidate": True, "updated_at": "2026-10-12 00:00:00Z",
        }
        row.update({k: v for k, v in fields.items() if k in MUTABLE})
        self._accounts[new_id] = row
        self.writes.append(("create", new_id, dict(fields)))
        return self.get_account(new_id)

    def update_account(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if account_id not in self._accounts:
            raise CRMError(404, "account not found", "PATCH", f"/accounts/{account_id}")
        mutable = {k: v for k, v in fields.items() if k in MUTABLE}
        if not mutable:
            raise CRMError(422, f"No mutable fields provided. Mutable: {sorted(MUTABLE)}", "PATCH", f"/accounts/{account_id}")
        self._validate(mutable)
        self._accounts[account_id].update(mutable)
        self.writes.append(("patch", account_id, dict(fields)))
        return {"account_id": account_id, "message": "updated", "fields": sorted(mutable)}

    # --------------------------------------------------------------- helpers
    def _validate(self, fields: dict[str, Any]) -> None:
        if "status" in fields and fields["status"] not in STATUSES:
            raise CRMError(422, f"status must be one of {sorted(STATUSES)}", "PATCH", "/accounts")
        if fields.get("parent_id") and fields["parent_id"] not in self._accounts:
            raise CRMError(422, "parent_id does not exist", "PATCH", "/accounts")

    def _with_parent_name(self, a: dict[str, Any]) -> dict[str, Any]:
        a = dict(a)
        pid = a.get("parent_id") or ""
        a["parent_name"] = self._accounts[pid]["name"] if pid in self._accounts else ""
        return a

    def raw(self, account_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._accounts[account_id])
