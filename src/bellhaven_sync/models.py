"""Shared data model. Everything that crosses a module boundary is defined here."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

# --------------------------------------------------------------------------- website


@dataclass(frozen=True)
class SiteLocation:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: tuple[str, ...]
    phone: str
    administrator: str
    source_url: str
    listed_in_directory: bool = True  # False when only discovered from the homepage

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["care_offerings"] = list(self.care_offerings)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SiteLocation":
        d = dict(d)
        d["care_offerings"] = tuple(d.get("care_offerings", ()))
        return cls(**d)


# --------------------------------------------------------------------------- CRM


@dataclass
class Account:
    account_id: str
    name: str
    parent_id: str = ""
    parent_name: str = ""
    billing_street: str = ""
    billing_city: str = ""
    billing_state: str = ""
    billing_zip: str = ""
    care_type: str = ""
    status: str = "Active"
    phone: str = ""
    lifetime_revenue: float = 0
    outstanding_ar: float = 0
    chow_current_account: str = ""
    duplicate_of_account: str = ""
    note: str = ""
    created_by_candidate: bool = False
    updated_at: str = ""

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Account":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: (v if v is not None else "") for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_parent_account(self) -> bool:
        return not self.billing_street and not self.billing_city and "parent" in self.name.lower()

    @property
    def is_superseded(self) -> bool:
        """Already pointed at a newer account (duplicate or change of ownership)."""
        return bool(self.duplicate_of_account or self.chow_current_account)

    @property
    def has_revenue_and_ar(self) -> bool:
        """The SOP condition: billing history AND open receivables."""
        return (self.lifetime_revenue or 0) > 0 and (self.outstanding_ar or 0) > 0


@dataclass
class Contact:
    contact_id: str
    account_id: str
    name: str
    title: str = ""
    email: str = ""
    phone: str = ""
    is_active: bool = True

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Contact":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: (v if v is not None else "") for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- proposals


class ProposalKind(StrEnum):
    CONFIRMED = "confirmed"              # informational, no actions
    RENAME = "rename"                    # patch name
    REPARENT = "reparent"                # patch parent_id (+ optional name)
    CHOW_REPARENT = "chow_reparent"      # create new under Bellhaven + chow pointer on old
    CREATE = "create"                    # new account under Bellhaven
    DUPLICATE = "duplicate"              # loser -> duplicate_of_account + Inactive
    CHOW_OUT = "chow_out"                # Bellhaven account superseded by other owner's account
    NOT_ON_WEBSITE = "not_on_website"    # -> Needs Review
    PARENT_ACQUIRED = "parent_acquired"  # acquired parent company -> Inactive


Priority = Literal["high", "medium", "low"]
PRIORITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

ProposalStatus = Literal["pending", "approved", "applied", "rejected", "failed", "stale"]
DECIDED_STATUSES: frozenset[str] = frozenset({"approved", "applied", "rejected", "failed"})

NEW_ACCOUNT_REF = "$new.account_id"  # placeholder resolved by apply.py after a create


@dataclass
class Action:
    """One CRM write. A `create` action binds the resulting id to `$new.account_id`
    for later actions in the same proposal."""

    op: Literal["patch", "create"]
    fields: dict[str, Any]
    account_id: str | None = None  # required for patch
    label: str = ""                # human description shown in the review UI

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "account_id": self.account_id, "fields": self.fields, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Action":
        return cls(op=d["op"], fields=dict(d["fields"]), account_id=d.get("account_id"), label=d.get("label", ""))


@dataclass
class Proposal:
    kind: ProposalKind
    priority: Priority
    title: str
    summary: str
    actions: list[Action]
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    subject_account_id: str | None = None
    site_slug: str | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = compute_fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "kind": str(self.kind),
            "priority": self.priority,
            "title": self.title,
            "summary": self.summary,
            "subject_account_id": self.subject_account_id,
            "site_slug": self.site_slug,
            "actions": [a.to_dict() for a in self.actions],
            "evidence": self.evidence,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        return cls(
            kind=ProposalKind(d["kind"]),
            priority=d["priority"],
            title=d["title"],
            summary=d["summary"],
            actions=[Action.from_dict(a) for a in d.get("actions", [])],
            evidence=d.get("evidence", {}),
            confidence=d.get("confidence", 1.0),
            subject_account_id=d.get("subject_account_id"),
            site_slug=d.get("site_slug"),
            fingerprint=d.get("fingerprint", ""),
        )


def compute_fingerprint(p: Proposal) -> str:
    """Stable identity of a proposal across runs. Excludes evidence and every `note`
    field so that dated notes never cause re-proposal."""
    payload = {
        "kind": str(p.kind),
        "subject": p.subject_account_id,
        "site": p.site_slug,
        "actions": [
            {
                "op": a.op,
                "account_id": a.account_id,
                "fields": {k: v for k, v in sorted(a.fields.items()) if k != "note"},
            }
            for a in p.actions
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
