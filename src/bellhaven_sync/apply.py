"""Execute an approved Proposal against the CRM.

This is the ONLY module that writes to the CRM, and only the review app calls it (on an
explicit reviewer Approve). Actions run in order; a `create` binds its new id to
`models.NEW_ACCOUNT_REF` (`$new.account_id`) for every later action in the same proposal.
Execution stops at the first failure; steps that already succeeded are kept in the result
so the reviewer can see a partial application. No retries here: the CRM client already
retries transient HTTP errors.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from .crm import CRM, CRMError
from .models import NEW_ACCOUNT_REF, Proposal

log = logging.getLogger(__name__)


@dataclass
class ApplyStep:
    op: str
    account_id: str | None
    fields: dict[str, Any]
    response: Any
    ok: bool
    error: str | None
    label: str = ""


@dataclass
class ApplyResult:
    ok: bool
    steps: list[ApplyStep] = field(default_factory=list)
    created_account_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [asdict(s) for s in self.steps],
            "created_account_id": self.created_account_id,
            "error": self.error,
        }


class UnresolvedReference(ValueError):
    """An action refers to `$new.account_id` before any account was created."""


def _resolve(value: Any, created_id: str | None) -> Any:
    """Substitute NEW_ACCOUNT_REF (exact value or inside strings), recursively."""
    if isinstance(value, str):
        if NEW_ACCOUNT_REF not in value:
            return value
        if created_id is None:
            raise UnresolvedReference(f"{NEW_ACCOUNT_REF} used before any account was created")
        return value.replace(NEW_ACCOUNT_REF, created_id)
    if isinstance(value, dict):
        return {k: _resolve(v, created_id) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve(v, created_id) for v in value]
    return value


def _jsonable(obj: Any) -> Any:
    """Make a CRM response storable as JSON (Account dataclasses -> dicts)."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def _describe(exc: Exception) -> str:
    if isinstance(exc, CRMError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def apply_proposal(proposal: Proposal, crm: CRM) -> ApplyResult:
    """Run `proposal.actions` in order against `crm`. Never raises for CRM/action errors;
    failures are reported in the returned ApplyResult."""
    steps: list[ApplyStep] = []
    created_id: str | None = None

    for action in proposal.actions:
        op = action.op
        account_id = action.account_id
        fields = dict(action.fields)
        try:
            account_id = _resolve(account_id, created_id)
            fields = _resolve(fields, created_id)
            if op == "create":
                account = crm.create_account(fields)
                response = _jsonable(account)
                new_id = getattr(account, "account_id", None) or (
                    response.get("account_id") if isinstance(response, dict) else None
                )
                if not new_id:
                    raise ValueError("create_account returned no account_id")
                created_id = new_id
                account_id = new_id
                log.info("created account %s (%s)", new_id, fields.get("name"))
            elif op == "patch":
                if not account_id:
                    raise ValueError("patch action has no account_id")
                response = _jsonable(crm.update_account(account_id, fields))
                log.info("patched account %s fields=%s", account_id, sorted(fields))
            else:
                raise ValueError(f"unknown action op {op!r}")
        except Exception as exc:  # stop at the first failure, keep what already happened
            error = _describe(exc)
            log.warning("apply %s failed at %s %s: %s", proposal.fingerprint[:12], op, account_id, error)
            steps.append(
                ApplyStep(op=op, account_id=account_id, fields=fields, response=None, ok=False, error=error, label=action.label)
            )
            return ApplyResult(ok=False, steps=steps, created_account_id=created_id, error=error)

        steps.append(
            ApplyStep(op=op, account_id=account_id, fields=fields, response=response, ok=True, error=None, label=action.label)
        )

    return ApplyResult(ok=True, steps=steps, created_account_id=created_id, error=None)
