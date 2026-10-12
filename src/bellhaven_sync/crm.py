"""CRM API client. The only module that talks to the CRM over HTTP.

Reads are used by the pipeline; writes (`create_account`, `update_account`) are used
ONLY by `apply.py` on reviewer approval.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import httpx

from .models import Account, Contact

log = logging.getLogger(__name__)

PAGE_SIZE = 200  # server caps page_size at 200
RETRY_STATUSES = {429, 500, 502, 503, 504}


class CRMError(RuntimeError):
    def __init__(self, status_code: int, detail: Any, method: str, url: str):
        super().__init__(f"{method} {url} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class CRM(Protocol):
    """Interface shared by the real client and tests' FakeCRM."""

    def list_accounts(self) -> list[Account]: ...
    def get_account(self, account_id: str) -> Account: ...
    def list_contacts(self, account_id: str | None = None) -> list[Contact]: ...
    def create_account(self, fields: dict[str, Any]) -> Account: ...
    def update_account(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...


class CRMClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None, max_retries: int = 4):
        self.api_url = base_url.rstrip("/")
        if not self.api_url.endswith("/api/v1"):
            self.api_url += "/api/v1"
        self._client = client or httpx.Client(timeout=30.0)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.max_retries = max_retries

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        url = f"{self.api_url}{path}"
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            resp = self._client.request(method, url, params=params, json=json, headers=self._headers)
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                log.warning("CRM %s %s -> %s, retrying in %.1fs", method, path, resp.status_code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail", resp.text)
                except ValueError:
                    detail = resp.text
                raise CRMError(resp.status_code, detail, method, url)
            return resp.json() if resp.content else {}
        raise AssertionError("unreachable")

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request("GET", path, params={**(params or {}), "page": page, "page_size": PAGE_SIZE})
            items = data.get("data", [])
            out.extend(items)
            total = data.get("total", len(out))
            if not items or len(out) >= total:
                return out
            page += 1

    # ----------------------------------------------------------------- reads
    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def list_accounts(self) -> list[Account]:
        return [Account.from_api(d) for d in self._paginate("/accounts")]

    def get_account(self, account_id: str) -> Account:
        return Account.from_api(self._request("GET", f"/accounts/{account_id}"))

    def list_contacts(self, account_id: str | None = None) -> list[Contact]:
        params = {"account_id": account_id} if account_id else None
        return [Contact.from_api(d) for d in self._paginate("/contacts", params)]

    # ---------------------------------------------------------------- writes
    def create_account(self, fields: dict[str, Any]) -> Account:
        data = self._request("POST", "/accounts", json=fields)
        # The sandbox returns either the created account or {"account_id": ...}; normalise.
        if "name" in data and "account_id" in data:
            return Account.from_api(data)
        return self.get_account(data["account_id"])

    def update_account(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)
