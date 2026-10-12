"""Reviewer-facing review app: FastAPI + server-rendered Jinja2 templates.

Nothing here writes to the CRM except the Approve route, which calls `apply.apply_proposal`
(the only CRM writer) exactly once per proposal: a proposal that is not pending/stale is
never applied again (HTTP 409). Templates and static files are located relative to this
module, so the app works from a source checkout and from an installed wheel.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

from ..apply import ApplyResult, apply_proposal
from ..config import Settings
from ..crm import CRM
from ..models import NEW_ACCOUNT_REF, Action, Proposal
from ..store import PartiallyAppliedError, Store, StoredProposal

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

APP_NAME = "Bellhaven CRM Sync — Review"
CONFIRMED = "confirmed"
STATUS_TABS: list[tuple[str, str]] = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("applied", "Applied"),
    ("rejected", "Rejected"),
    ("failed", "Failed"),
    ("stale", "Stale"),
]
TAB_KEYS = [k for k, _ in STATUS_TABS] + [CONFIRMED]
APPROVABLE = frozenset({"pending", "stale"})
REOPENABLE = frozenset({"rejected", "failed"})
SOP_KINDS = frozenset({"reparent", "chow_reparent", "duplicate", "chow_out"})
DUPLICATE = "duplicate"
SURVIVOR_TOKEN = "{survivor}"  # replaced in the browser by the checked survivor radio (base.html)
NEW_ACCOUNT_TEXT = "the newly created account"
ID_FIELDS = frozenset({"parent_id", "chow_current_account", "duplicate_of_account", "account_id"})

KIND_LABELS = {
    "confirmed": "Confirmed",
    "rename": "Rename",
    "reparent": "Reparent",
    "chow_reparent": "CHOW reparent",
    "create": "Create",
    "duplicate": "Duplicate",
    "chow_out": "CHOW out",
    "not_on_website": "Not on website",
    "parent_acquired": "Parent acquired",
    "field_drift": "Field drift",
}
ROLE_LABELS = {
    "subject": "CRM · subject",
    "survivor": "CRM · survivor (kept)",
    "loser": "CRM · duplicate (retire)",
    "related": "CRM · possibly related",
    "new_owner": "CRM · new owner",
    "old": "CRM · old owner record",
    "parent": "CRM · parent account",
}
TAB_HELP = {
    "pending": "Proposed CRM corrections waiting for a decision. Nothing is written until you click Approve.",
    "approved": "Approved and currently being applied (normally transient).",
    "applied": "Approved and written to the CRM. Each entry keeps the API responses.",
    "rejected": "Rejected by a reviewer. Re-runs will not propose these again unless reopened.",
    "failed": "Approved, but the CRM rejected a write. Open one to see which step failed.",
    "stale": "Still pending, but the latest pipeline run no longer produced them (the CRM or website changed).",
    "confirmed": "Website locations that already match the CRM. Informational only: nothing to change.",
}
FLASH_MESSAGES = {
    "applied": ("ok", "Approved and applied to the CRM."),
    "failed": ("error", "Approved, but applying to the CRM failed. See the failing step below."),
    "rejected": ("info", "Rejected. Nothing was written to the CRM."),
    "reopened": ("info", "Reopened: the proposal is back in the pending queue."),
}
SCORE_MAX = {"street": 45, "zip": 10, "city": 10, "phone": 20, "admin": 15, "administrator": 15, "name": 25, "locality": 10}
SCORE_LABELS = {
    "street": "Street address",
    "zip": "ZIP",
    "city": "City",
    "phone": "Phone",
    "admin": "Administrator is a contact",
    "administrator": "Administrator is a contact",
    "name": "Name similarity",
    "locality": "Locality in CRM name",
}

# ----------------------------------------------------------------- display normalization
# Display-only comparisons for highlighting mismatching cells. The matcher has its own,
# authoritative normalization; this only decides which cells to colour.
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_STREET_ABBR = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "pike": "pk", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "circle": "cir", "terrace": "ter", "trail": "trl", "square": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w", "northwest": "nw", "northeast": "ne",
    "southwest": "sw", "southeast": "se", "saint": "st", "suite": "ste",
}
_NAME_ABBR = {"rehab": "rehabilitation", "centre": "center"}
_NAME_STOP = {"the", "of", "at"}
CARE_MAP = {
    "short-term rehabilitation & nursing": "skilled nursing",
    "memory support": "memory care",
    "assisted living": "assisted living",
}


def _words(s: str, abbr: dict[str, str], stop: set[str] | frozenset[str] = frozenset()) -> str:
    s = (s or "").lower().replace("&", " and ").replace("health care", "healthcare")
    s = _NON_ALNUM.sub(" ", s)
    return " ".join(abbr.get(w, w) for w in s.split() if w not in stop)


def _norm_name(s: str) -> str:
    return _words(s, _NAME_ABBR, _NAME_STOP)


def _norm_street(s: str) -> str:
    return _words(s, _STREET_ABBR)


def _digits(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))[-10:]


def _person(s: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", (s or "").lower()).split())


def money(v: Any) -> str:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    return f"${f:,.0f}" if f == int(f) else f"${f:,.2f}"


def timestamp(v: Any) -> str:
    """'2026-10-12T03:10:05+00:00' -> '2026-10-12 03:10 UTC' (store timestamps are UTC)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", str(v or ""))
    return f"{m.group(1)} {m.group(2)} UTC" if m else str(v or "")


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _cell(text: Any = "", cls: str = "", **extra: Any) -> dict[str, Any]:
    return {"text": "" if text is None else str(text), "cls": cls, **extra}


def _copies(n: int) -> str:
    return f"{n} cop{'y' if n == 1 else 'ies'}"


def raw_cluster(p: Proposal) -> dict[str, Any] | None:
    """`evidence["cluster"]` of a one-proposal-per-cluster duplicate, normalized, or None.

    Older stored duplicate proposals (one per loser) have no cluster and keep the old layout.
    `recommended` always names one of the candidates (falls back to the first/top-ranked)."""
    if str(p.kind) != DUPLICATE or not isinstance(p.evidence, dict):
        return None
    cl = p.evidence.get("cluster")
    if not isinstance(cl, dict):
        return None
    cands = [c for c in (cl.get("candidates") or []) if isinstance(c, dict) and c.get("account_id")]
    if not cands:
        return None
    ids = [c["account_id"] for c in cands]
    rec = cl.get("recommended") if cl.get("recommended") in ids else ids[0]
    return {**cl, "candidates": cands, "recommended": rec, "by_id": dict(zip(ids, cands))}


def _cand_label(c: dict[str, Any]) -> str:
    return f"{c.get('name') or c['account_id']} ({c['account_id']})"


class SurvivorChoiceError(ValueError):
    """The posted survivor cannot be honoured; nothing is decided or written."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def resolve_survivor(p: Proposal, survivor: str) -> tuple[Proposal, dict[str, Any], str]:
    """Pick the actions to apply for a reviewer-chosen duplicate survivor.

    Returns (proposal to apply, extra keys for the stored apply result, reviewer-note prefix).
    The stored proposal is never modified: a different survivor only yields a copy with
    rebuilt actions (same fingerprint), and the apply result records the deviation."""
    cl = raw_cluster(p)
    if cl is None:
        if survivor and str(p.kind) == DUPLICATE:
            raise SurvivorChoiceError("This duplicate proposal has no candidate list, so a survivor cannot be chosen.")
        return p, {}, ""
    rec = cl["recommended"]
    if not survivor or survivor == rec:
        return p, {"chosen_survivor": rec, "recommended_survivor": rec}, ""
    if survivor not in cl["by_id"]:
        raise SurvivorChoiceError(f"{survivor!r} is not one of the candidate accounts of this duplicate cluster.")
    try:
        from ..proposals import build_duplicate_group_actions  # lazy: the engine owns it; tests monkeypatch it

        built = build_duplicate_group_actions(p.evidence, survivor)
        actions = [a if isinstance(a, Action) else Action.from_dict(a) for a in built or []]
    except Exception as exc:
        log.exception("rebuilding duplicate actions for survivor %s failed", survivor)
        raise SurvivorChoiceError(f"Could not rebuild the actions for survivor {survivor}: {type(exc).__name__}: {exc}.", 500) from exc
    if not actions:
        raise SurvivorChoiceError(f"Rebuilding the actions for survivor {survivor} produced no CRM writes.", 500)
    extra = {"chosen_survivor": survivor, "recommended_survivor": rec, "applied_actions": [a.to_dict() for a in actions]}
    return replace(p, actions=actions), extra, f"survivor={survivor} ({cl['by_id'][survivor].get('name') or ''})"


# ------------------------------------------------------------------------ view builders
class Views:
    """Turns stored proposals into plain dicts for the templates."""

    def __init__(self, settings: Settings):
        self.settings = settings

    # -------------------------------------------------------------- helpers
    def crm_url(self, account_id: Any) -> str | None:
        if not account_id or not isinstance(account_id, str) or NEW_ACCOUNT_REF in account_id:
            return None
        return self.settings.crm_browser_url(account_id)

    def is_target_parent(self, acct: dict[str, Any]) -> bool:
        if self.settings.parent_account_id:
            return acct.get("parent_id") == self.settings.parent_account_id
        return (acct.get("parent_name") or "") == self.settings.parent_account_name

    @staticmethod
    def accounts(evidence: dict[str, Any]) -> list[dict[str, Any]]:
        return [a for a in (evidence.get("crm_accounts") or []) if isinstance(a, dict)]

    def id_names(self, evidence: dict[str, Any], known: dict[str, str] | None = None) -> dict[str, str]:
        """account_id -> name from this evidence, falling back to `known` (other proposals)."""
        names: dict[str, str] = dict(known or {})
        for a in self.accounts(evidence):
            if a.get("account_id"):
                names[a["account_id"]] = a.get("name", "")
            if a.get("parent_id") and a.get("parent_name"):
                names[a["parent_id"]] = a["parent_name"]
        cluster = evidence.get("cluster") if isinstance(evidence, dict) else None
        for c in (cluster.get("candidates") or []) if isinstance(cluster, dict) else []:
            if isinstance(c, dict) and c.get("account_id"):
                names.setdefault(c["account_id"], c.get("name", ""))
                if c.get("parent_id") and c.get("parent_name"):
                    names.setdefault(c["parent_id"], c["parent_name"])
        if self.settings.parent_account_id:
            names.setdefault(self.settings.parent_account_id, self.settings.parent_account_name)
        return names

    def id_cell(self, account_id: Any, names: dict[str, str], cls: str = "") -> dict[str, Any]:
        if not account_id:
            return _cell("", cls)
        return _cell(account_id, cls, link=self.crm_url(account_id), sub=names.get(account_id, ""), mono=True)

    # ------------------------------------------------------------ comparison
    def comparison(self, evidence: dict[str, Any], known: dict[str, str] | None = None) -> dict[str, Any]:
        site = evidence.get("site") if isinstance(evidence.get("site"), dict) else None
        contacts = evidence.get("contacts") or {}
        names = self.id_names(evidence, known)
        # Parent accounts have no address/contacts: list them as links above the table
        # instead of spending a mostly-empty column on each.
        all_accts = self.accounts(evidence)
        accts = [a for a in all_accts if a.get("role") != "parent"]
        parents = [
            {"name": a.get("name") or a.get("account_id"), "account_id": a.get("account_id"), "link": self.crm_url(a.get("account_id"))}
            for a in all_accts
            if a.get("role") == "parent"
        ]

        columns: list[dict[str, Any]] = []
        if site:
            columns.append(
                {"kind": "site", "role_label": "Website says", "name": site.get("name"), "link": site.get("source_url"), "account_id": None}
            )
        for a in accts:
            role = a.get("role") or "related"
            columns.append(
                {
                    "kind": "crm",
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, f"CRM · {role}"),
                    "name": a.get("name") or a.get("account_id"),
                    "link": self.crm_url(a.get("account_id")),
                    "account_id": a.get("account_id"),
                }
            )

        def cmp_cls(a: dict[str, Any], same: bool) -> str:
            if not site or a.get("role") == "parent":
                return ""
            return "same" if same else "diff"

        rows: list[dict[str, Any]] = []

        def add(label: str, site_cell: dict[str, Any], crm_cells: list[dict[str, Any]], sop: bool = False) -> None:
            cells = [site_cell, *crm_cells] if site else list(crm_cells)
            if any(c.get("text") or c.get("entries") for c in cells):
                rows.append({"label": label, "cells": cells, "sop": sop})

        s = site or {}
        add(
            "Name",
            _cell(s.get("name", "")),
            [_cell(a.get("name"), cmp_cls(a, _norm_name(a.get("name", "")) == _norm_name(s.get("name", "")))) for a in accts],
        )
        add(
            "Street",
            _cell(s.get("street", "")),
            [
                _cell(a.get("billing_street"), cmp_cls(a, _norm_street(a.get("billing_street", "")) == _norm_street(s.get("street", ""))))
                for a in accts
            ],
        )

        def csz(city: str, state: str, zip_: str) -> str:
            left = ", ".join(x for x in (city, state) if x)
            return f"{left} {zip_}".strip()

        add(
            "City, State ZIP",
            _cell(csz(s.get("city", ""), s.get("state", ""), s.get("zip", ""))),
            [
                _cell(
                    csz(a.get("billing_city", ""), a.get("billing_state", ""), a.get("billing_zip", "")),
                    cmp_cls(
                        a,
                        (a.get("billing_city", "").strip().lower(), a.get("billing_state", "").strip().upper(), str(a.get("billing_zip", ""))[:5])
                        == (s.get("city", "").strip().lower(), s.get("state", "").strip().upper(), str(s.get("zip", ""))[:5]),
                    ),
                )
                for a in accts
            ],
        )
        offerings = [str(o) for o in (s.get("care_offerings") or [])]
        mapped = {CARE_MAP.get(o.strip().lower(), o.strip().lower()) for o in offerings}
        add(
            "Care",
            _cell(", ".join(offerings), sub=("CRM equivalent: " + ", ".join(sorted(m.title() for m in mapped))) if offerings else ""),
            [_cell(a.get("care_type"), cmp_cls(a, (a.get("care_type") or "").strip().lower() in mapped)) for a in accts],
        )
        add(
            "Phone",
            _cell(s.get("phone", "")),
            [_cell(a.get("phone"), cmp_cls(a, bool(_digits(a.get("phone"))) and _digits(a.get("phone")) == _digits(s.get("phone")))) for a in accts],
        )

        admin = s.get("administrator", "")
        contact_cells = []
        for a in accts:
            rows_ = contacts.get(a.get("account_id"), []) if isinstance(contacts, dict) else []
            items, hit = [], False
            for c in rows_:
                if not isinstance(c, dict):
                    continue
                matches = bool(admin) and _person(c.get("name", "")) == _person(admin) and c.get("is_active", True)
                hit = hit or matches
                cls = "match" if matches else ("inactive" if not c.get("is_active", True) else "")
                items.append({"text": c.get("name", ""), "sub": c.get("title", "") + ("" if c.get("is_active", True) else " (inactive)"), "cls": cls})
            text = "" if items else "No contacts"
            cell = _cell(text, cmp_cls(a, hit) if admin else "", entries=items)
            if not items:
                cell["muted"] = True
            contact_cells.append(cell)
        add("Administrator / contacts", _cell(admin), contact_cells)

        add(
            "Parent",
            _cell(self.settings.parent_account_name.replace(" (Parent Account)", "") + " (website owner)" if site else ""),
            [
                _cell(
                    a.get("parent_name") or a.get("parent_id") or "(no parent)",
                    cmp_cls(a, self.is_target_parent(a)),
                    link=self.crm_url(a.get("parent_id")),
                )
                for a in accts
            ],
        )
        status_site = ""
        if site:
            status_site = "Listed in directory" if s.get("listed_in_directory", True) else "Homepage only (not in directory)"
        add(
            "Status",
            _cell(status_site, "warn" if site and not s.get("listed_in_directory", True) else ""),
            [_cell(a.get("status"), "" if (a.get("status") or "Active") == "Active" else "warn") for a in accts],
        )

        def billing_cls(a: dict[str, Any], v: Any) -> str:
            both = _num(a.get("lifetime_revenue")) > 0 and _num(a.get("outstanding_ar")) > 0
            return "sop" if both else ("info" if _num(v) > 0 else "")

        add("Lifetime revenue", _cell(""), [_cell(money(a.get("lifetime_revenue")), billing_cls(a, a.get("lifetime_revenue"))) for a in accts], sop=True)
        add("Outstanding AR", _cell(""), [_cell(money(a.get("outstanding_ar")), billing_cls(a, a.get("outstanding_ar"))) for a in accts], sop=True)
        add("CHOW → current account", _cell(""), [self.id_cell(a.get("chow_current_account"), names, "warn" if a.get("chow_current_account") else "") for a in accts])
        add("Duplicate of", _cell(""), [self.id_cell(a.get("duplicate_of_account"), names, "warn" if a.get("duplicate_of_account") else "") for a in accts])
        add("Existing note", _cell(""), [_cell(a.get("note"), "note") for a in accts])
        return {"columns": columns, "rows": rows, "has_site": bool(site), "parents": parents}

    # ------------------------------------------------------------------ score
    @staticmethod
    def score(score: Any) -> dict[str, Any] | None:
        if not isinstance(score, dict) or not score:
            return None
        comps = []
        for k, v in (score.get("components") or {}).items():
            pts = _num(v)
            mx = SCORE_MAX.get(k) or max(pts, 1.0)
            comps.append(
                {
                    "key": k,
                    "label": SCORE_LABELS.get(k, k.replace("_", " ").capitalize()),
                    "points": f"{pts:g}",
                    "max": f"{mx:g}",
                    "pct": max(0, min(100, round(pts / mx * 100))) if mx else 0,
                }
            )
        flags = [
            {"label": label, "value": bool(score.get(key)), "cls": "yes" if score.get(key) else "no"}
            for key, label in (("street_match", "Street"), ("phone_match", "Phone"), ("admin_match", "Administrator"))
            if key in score
        ]
        sim = score.get("name_similarity")
        return {
            "total": f"{_num(score.get('total')):g}",
            "components": comps,
            "flags": flags,
            "name_similarity": f"{_num(sim):g}" if sim is not None else None,
            "address_differs": bool(score.get("address_differs")),
        }

    # ---------------------------------------------------------------- actions
    def value_view(self, key: str, value: Any, names: dict[str, str]) -> dict[str, Any]:
        if isinstance(value, str):
            if value == NEW_ACCOUNT_REF:
                return {"is_ref": True, "parts": None}
            if key in ID_FIELDS and value:
                return {"id": value, "link": self.crm_url(value), "sub": names.get(value, "")}
            return {"parts": value.split(NEW_ACCOUNT_REF)}
        return {"parts": [json.dumps(value)]}

    def actions(self, p: Proposal, known: dict[str, str] | None = None) -> list[dict[str, Any]]:
        names = self.id_names(p.evidence, known)
        by_id = {a.get("account_id"): a for a in self.accounts(p.evidence)}
        cl = raw_cluster(p)
        for cid, c in (cl["by_id"] if cl else {}).items():
            by_id.setdefault(cid, c)
        out = []
        for i, a in enumerate(p.actions, 1):
            target = a.account_id
            acct = by_id.get(target) if target else None
            fields = []
            for k, v in a.fields.items():
                old = None
                if a.op == "patch" and acct is not None and k in acct and k != "note":
                    old_v = acct.get(k)
                    if str(old_v or "") != str(v or ""):
                        old = str(old_v) if old_v not in (None, "") else "(empty)"
                fields.append({"key": k, "old": old, "value": self.value_view(k, v, names)})
            if a.op == "create":
                default_label, path = f"Create account {a.fields.get('name', '')}".strip(), "/api/v1/accounts"
            else:
                default_label, path = f"Update {names.get(target or '', target)}", f"/api/v1/accounts/{target}"
            out.append(
                {
                    "n": i,
                    "op": a.op,
                    "label": a.label or default_label,
                    "method": "POST" if a.op == "create" else "PATCH",
                    "path": path,
                    "target": target,
                    "target_is_new": target == NEW_ACCOUNT_REF,
                    "target_name": names.get(target or "", ""),
                    "target_link": self.crm_url(target),
                    "fields": fields,
                    "payload": json.dumps(a.fields, indent=2, ensure_ascii=False),
                }
            )
        return out

    @staticmethod
    def confirm_text(p: Proposal, survivor_label: str | None = None) -> str:
        cl = raw_cluster(p)
        if cl is not None:
            label = survivor_label or _cand_label(cl["by_id"][cl["recommended"]])
            lines = ["Approve and write this duplicate merge to the CRM?", "", p.title, "", f"Survivor (kept): {label}"]
            lines.append(f"The other {_copies(len(cl['candidates']) - 1)} will point at the survivor via duplicate_of_account.")
            if cl.get("tie"):
                lines.append("(Tie: there was no automatic recommendation.)")
            return "\n".join(lines)
        n = len(p.actions)
        lines = [f"Approve and write {n} change{'s' if n != 1 else ''} to the CRM?", "", p.title]
        lines += [f"  {i}. {a.label or a.op}" for i, a in enumerate(p.actions, 1)]
        return "\n".join(lines)

    # ----------------------------------------------------- duplicate cluster
    def cluster(self, p: Proposal) -> dict[str, Any] | None:
        cl = raw_cluster(p)
        if cl is None:
            return None
        bh_parent = cl.get("bellhaven_parent_id")
        rows = []
        for c in cl["candidates"]:
            rev, ar = _num(c.get("lifetime_revenue")), _num(c.get("outstanding_ar"))
            city = ", ".join(x for x in (c.get("billing_city"), c.get("billing_state")) if x)
            where = ", ".join(x for x in (c.get("billing_street"), f"{city} {c.get('billing_zip') or ''}".strip()) if x)
            sim = c.get("name_similarity")
            rows.append(
                {
                    "account_id": c["account_id"],
                    "name": c.get("name") or c["account_id"],
                    "label": _cand_label(c),
                    "link": self.crm_url(c["account_id"]),
                    "status": c.get("status") or "",
                    "parent": c.get("parent_name") or c.get("parent_id") or "(no parent)",
                    "parent_link": self.crm_url(c.get("parent_id")),
                    "parent_ok": (c.get("parent_id") == bh_parent) if bh_parent else self.is_target_parent(c),
                    "street": where,
                    "care_type": c.get("care_type") or "",
                    "revenue": money(rev),
                    "ar": money(ar),
                    "both": rev > 0 and ar > 0,
                    "contacts": [
                        {"name": x.get("name", ""), "title": x.get("title", ""), "inactive": not x.get("is_active", True)}
                        for x in (c.get("contacts") or [])
                        if isinstance(x, dict)
                    ],
                    "phone_match": bool(c.get("phone_match")),
                    "admin_match": bool(c.get("admin_match")),
                    "name_similarity": f"{_num(sim):g}" if sim is not None else None,
                    "rank": c.get("rank"),
                    "rank_reason": c.get("rank_reason") or "",
                    "recommended": c["account_id"] == cl["recommended"],
                }
            )
        n = len(rows) - 1
        return {
            "candidates": rows,
            "recommended": cl["recommended"],
            "tie": bool(cl.get("tie")),
            "n_copies": n,
            "summary": f"Approve will mark {_copies(n)} Inactive pointing at the survivor, "
            "and update the survivor to the website identity if needed.",
        }

    # -------------------------------------------------------------------- SOP
    def sop(self, p: Proposal) -> dict[str, Any] | None:
        accts = [a for a in self.accounts(p.evidence) if a.get("role") != "parent"]
        kind = str(p.kind)
        any_billing = any(_num(a.get("lifetime_revenue")) > 0 or _num(a.get("outstanding_ar")) > 0 for a in accts)
        if kind not in SOP_KINDS and not any_billing:
            return None
        rows = []
        for a in accts:
            rev, ar = _num(a.get("lifetime_revenue")), _num(a.get("outstanding_ar"))
            both = rev > 0 and ar > 0
            if both:
                verdict = "Revenue AND open AR: billing history must stay on this account (no parent move, not hidden)."
            elif rev > 0:
                verdict = "Revenue but no open AR: a direct change is allowed."
            else:
                verdict = "No billing history."
            rows.append(
                {
                    "name": a.get("name"),
                    "account_id": a.get("account_id"),
                    "link": self.crm_url(a.get("account_id")),
                    "role": ROLE_LABELS.get(a.get("role") or "", a.get("role") or ""),
                    "revenue": money(rev),
                    "ar": money(ar),
                    "both": both,
                    "verdict": verdict,
                }
            )
        headline = {
            "chow_reparent": "SOP applies: the account has revenue AND open AR, so its parent is NOT changed. "
            "A new account is created under Bellhaven and the old account points to it via chow_current_account.",
            "reparent": "SOP check: no open AR on the moving account, so the parent can be changed directly.",
            "duplicate": "SOP check: duplicates are retired with duplicate_of_account → survivor. A loser that has "
            "revenue AND open AR goes to Needs Review instead of Inactive so billing history stays visible.",
            "chow_out": "SOP applies: the Bellhaven account keeps its parent and status; it gets chow_current_account → "
            "the new owner's account plus a note.",
        }.get(kind, "Billing facts for the accounts involved.")
        with_billing = [r for r in rows if r["revenue"] != "$0" or r["ar"] != "$0"]
        without = [r for r in rows if r not in with_billing]
        return {
            "headline": headline,
            "rows": with_billing,
            "zero": without,
            "total": len(rows),
            "strong": any(r["both"] for r in rows),
            "compact": not with_billing,
        }

    # ---------------------------------------------------------- apply result
    def apply_result(self, sp: StoredProposal, known: dict[str, str] | None = None) -> dict[str, Any] | None:
        r = sp.apply_result
        if not isinstance(r, dict):
            return None
        names = self.id_names(sp.proposal.evidence, known)
        created = r.get("created_account_id")
        steps = []
        for s in r.get("steps") or []:
            if not isinstance(s, dict):
                continue
            resp = s.get("response")
            summary = ""
            if isinstance(resp, dict):
                if "message" in resp:
                    summary = f"{resp.get('message')}: {', '.join(map(str, resp.get('fields') or []))}".rstrip(": ")
                elif resp.get("account_id"):
                    summary = str(resp.get("name") or "created")
                    if resp.get("name"):
                        names.setdefault(str(resp["account_id"]), str(resp["name"]))
            steps.append(
                {
                    "op": s.get("op"),
                    "label": s.get("label") or s.get("op"),
                    "account_id": s.get("account_id"),
                    "link": self.crm_url(s.get("account_id")),
                    "ok": bool(s.get("ok")),
                    "error": s.get("error"),
                    "summary": summary,
                    "response_json": json.dumps(resp, indent=2, ensure_ascii=False, default=str) if resp is not None else "",
                }
            )
        link_ids: list[str] = []
        for i in [created, *(s["account_id"] for s in steps), sp.proposal.subject_account_id]:
            if i and i not in link_ids and self.crm_url(i):
                link_ids.append(i)
        links = []
        for i in link_ids:
            label = names.get(i) or ""
            if i == created:
                label = f"New account: {label}" if label else "New account"
            links.append({"id": i, "label": label or i, "link": self.crm_url(i), "created": i == created})
        ok_steps = [s for s in steps if s["ok"]]
        applied_actions = r.get("applied_actions")
        survivor = None
        if r.get("chosen_survivor"):
            cs, rec = str(r["chosen_survivor"]), r.get("recommended_survivor")
            survivor = {
                "id": cs,
                "name": names.get(cs, ""),
                "link": self.crm_url(cs),
                "overridden": bool(rec) and rec != cs,
                "recommended_id": rec,
                "recommended_name": names.get(rec or "", ""),
            }
        return {
            "survivor": survivor,
            "ok": bool(r.get("ok")),
            "error": r.get("error"),
            "created": created,
            "created_link": self.crm_url(created),
            "steps": steps,
            "links": links,
            "partial": (not r.get("ok")) and bool(ok_steps),
            "ok_count": len(ok_steps),
            "total": len(applied_actions) if isinstance(applied_actions, list) else len(sp.proposal.actions),
        }

    # ------------------------------------------------------------- queue rows
    def row(self, sp: StoredProposal) -> dict[str, Any]:
        p = sp.proposal
        ev = p.evidence or {}
        site = ev.get("site") if isinstance(ev.get("site"), dict) else {}
        names = self.id_names(ev)
        kind = str(p.kind)
        err = sp.apply_result.get("error") if isinstance(sp.apply_result, dict) else None
        cl = raw_cluster(p)
        cluster_line = ""
        if cl is not None and "recommended" not in (p.summary or "").lower():
            rec = cl["by_id"][cl["recommended"]]
            cluster_line = f"{_copies(len(cl['candidates']) - 1)} · recommended: {rec.get('name') or rec['account_id']}"
            if cl.get("tie"):
                cluster_line += " (tie: pick the survivor)"
        return {
            "fp": p.fingerprint,
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "priority": p.priority,
            "status": sp.status,
            "title": p.title,
            "summary": p.summary,
            "cluster_line": cluster_line,
            "subject_id": p.subject_account_id,
            "subject_name": names.get(p.subject_account_id or "", ""),
            "subject_link": self.crm_url(p.subject_account_id),
            "site_url": site.get("source_url"),
            "site_name": site.get("name") or p.site_slug,
            "n_actions": len(p.actions),
            "decided_by": sp.decided_by,
            "decided_at": sp.decided_at,
            "applied_at": sp.applied_at,
            "error": err,
            "approvable": sp.status in APPROVABLE and kind != CONFIRMED,
            "reopenable": sp.status in REOPENABLE,
            "confirm": self.confirm_text(p),
        }


# ------------------------------------------------------------------------------- app
def _foreign_origin(request: Request) -> bool:
    """True for a cross-site form POST (defence in depth: the app writes to the CRM)."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return False
    return urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower()


def create_app(settings: Settings, store: Store, crm: CRM) -> FastAPI:
    app = FastAPI(title=APP_NAME, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    views = Views(settings)
    # One lock serialises store access and, crucially, the check-decide-apply sequence of
    # Approve, so a double click can never apply a proposal twice.
    lock = threading.RLock()

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        undefined=ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = money
    env.filters["ts"] = timestamp
    env.globals.update(
        app_name=APP_NAME,
        crm_url=views.crm_url,
        reviewer=settings.reviewer_name,
        crm_host=urlsplit(settings.base_url).netloc or settings.base_url,
        kind_labels=KIND_LABELS,
    )

    # ------------------------------------------------------------- rendering
    def tab_counts(all_rows: list[StoredProposal]) -> dict[str, int]:
        counts = {k: 0 for k in TAB_KEYS}
        for sp in all_rows:
            if str(sp.proposal.kind) == CONFIRMED:
                counts[CONFIRMED] += 1
            elif sp.status in counts:
                counts[sp.status] += 1
        return counts

    def in_tab(sp: StoredProposal, tab: str) -> bool:
        if tab == CONFIRMED:
            return str(sp.proposal.kind) == CONFIRMED
        return sp.status == tab and str(sp.proposal.kind) != CONFIRMED

    def nav_tabs(counts: dict[str, int]) -> list[dict[str, Any]]:
        tabs = [{"key": k, "label": label, "count": counts.get(k, 0)} for k, label in STATUS_TABS]
        tabs.append({"key": CONFIRMED, "label": "Confirmed (no change)", "count": counts.get(CONFIRMED, 0)})
        return tabs

    def render(name: str, *, status_code: int = 200, active_tab: str | None = None, **ctx: Any) -> HTMLResponse:
        with lock:
            counts = tab_counts(store.list())
        html = env.get_template(name).render(tabs=nav_tabs(counts), active_tab=active_tab, counts=counts, **ctx)
        return HTMLResponse(html, status_code=status_code)

    def flash_for(code: str | None, **extra: Any) -> dict[str, Any] | None:
        if not code or code not in FLASH_MESSAGES:
            return None
        level, text = FLASH_MESSAGES[code]
        return {"level": level, "text": text, **extra}

    def render_detail(sp: StoredProposal, *, flash: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
        p = sp.proposal
        ev = p.evidence or {}
        tab = CONFIRMED if str(p.kind) == CONFIRMED else sp.status
        with lock:  # names of accounts mentioned in any proposal (e.g. the Bellhaven parent)
            known: dict[str, str] = {}
            for other in store.list():
                known.update(views.id_names(other.proposal.evidence or {}))
        row = views.row(sp)
        cluster = views.cluster(p)
        # After a reviewer picked a non-recommended survivor, show what was actually applied.
        applied = sp.apply_result.get("applied_actions") if isinstance(sp.apply_result, dict) else None
        shown, actions_overridden = p, False
        if isinstance(applied, list) and applied:
            try:
                shown, actions_overridden = replace(p, actions=[Action.from_dict(a) for a in applied]), True
            except (KeyError, TypeError):
                log.warning("unreadable applied_actions for %s", p.fingerprint[:12])
        return render(
            "detail.html",
            status_code=status_code,
            active_tab=tab,
            nav="queue",
            flash=flash,
            sp=sp,
            p=p,
            row=row,
            cluster=cluster,
            confirm=views.confirm_text(p, SURVIVOR_TOKEN) if cluster else row["confirm"],
            actions_overridden=actions_overridden,
            ev=ev,
            site=ev.get("site") if isinstance(ev.get("site"), dict) else None,
            cmp=views.comparison(ev, known),
            score=views.score(ev.get("score")),
            actions=views.actions(shown, known),
            sop=views.sop(p),
            result=views.apply_result(sp, known),
            rule=ev.get("rule"),
            notes=[n for n in (ev.get("notes") or []) if n],
            about_text=ev.get("about_text"),
            approvable=sp.status in APPROVABLE and str(p.kind) != CONFIRMED,
            reopenable=sp.status in REOPENABLE,
        )

    def not_found(what: str) -> HTMLResponse:
        return render("message.html", status_code=404, title="Not found", message=f"No proposal {what!r}.")

    def conflict(sp: StoredProposal, verb: str) -> HTMLResponse:
        flash = {
            "level": "warn",
            "text": f"Not {verb}: this proposal is already {sp.status}. Nothing was written to the CRM.",
        }
        return render_detail(sp, flash=flash, status_code=409)

    def refused() -> Response:
        return PlainTextResponse("Cross-origin POST refused.", status_code=403)

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def queue(status: str = "pending", kind: str = "", msg: str = "", fp: str = "") -> HTMLResponse:
        tab = status if status in TAB_KEYS else "pending"
        with lock:
            all_rows = store.list()
            flashed = store.get(fp) if fp else None
        in_view = [sp for sp in all_rows if in_tab(sp, tab)]
        kind_counts: dict[str, int] = {}
        for sp in in_view:
            kind_counts[str(sp.proposal.kind)] = kind_counts.get(str(sp.proposal.kind), 0) + 1
        shown = [sp for sp in in_view if not kind or str(sp.proposal.kind) == kind]
        flash = flash_for(msg)
        if flash and flashed is not None:
            flash.update(subject=flashed.proposal.title, link=f"/proposals/{flashed.fingerprint}")
        return render(
            "queue.html",
            active_tab=tab,
            nav="queue",
            flash=flash,
            tab_title=dict(STATUS_TABS).get(tab, "Confirmed (no change)"),
            tab_help=TAB_HELP.get(tab, ""),
            kind=kind,
            total=len(in_view),
            kind_chips=[{"kind": k, "label": KIND_LABELS.get(k, k), "count": n} for k, n in sorted(kind_counts.items())],
            rows=[views.row(sp) for sp in shown],
            show_status=tab == CONFIRMED,
        )

    @app.get("/proposals/{fingerprint}", response_class=HTMLResponse)
    def detail(fingerprint: str, msg: str = "") -> HTMLResponse:
        with lock:
            sp = store.get(fingerprint)
        if sp is None:
            return not_found(fingerprint)
        return render_detail(sp, flash=flash_for(msg))

    @app.post("/proposals/{fingerprint}/approve")
    def approve(
        request: Request,
        fingerprint: str,
        note: Annotated[str, Form()] = "",
        survivor: Annotated[str, Form()] = "",
    ) -> Response:
        if _foreign_origin(request):
            return refused()
        with lock:
            sp = store.get(fingerprint)
            if sp is None:
                return not_found(fingerprint)
            if sp.status not in APPROVABLE:
                return conflict(sp, "applied")
            try:  # a duplicate cluster may be approved with a reviewer-chosen survivor
                to_apply, extra, note_prefix = resolve_survivor(sp.proposal, survivor.strip())
            except SurvivorChoiceError as exc:
                msg = f"{exc} Nothing was decided and nothing was written to the CRM."
                return render("message.html", status_code=exc.status_code, title="Not applied", message=msg)
            note = note.strip()
            if note_prefix:
                note = f"{note_prefix}; {note}" if note else note_prefix
            store.decide(fingerprint, "approved", by=settings.reviewer_name, note=note)
            try:
                result = apply_proposal(to_apply, crm)
            except Exception as exc:  # apply_proposal reports its own errors; last-resort guard
                log.exception("apply crashed for %s", fingerprint)
                result = ApplyResult(ok=False, steps=[], created_account_id=None, error=f"{type(exc).__name__}: {exc}")
            outcome = {**result.to_dict(), **extra}
            if result.ok:
                store.mark_applied(fingerprint, outcome)
            else:
                store.mark_failed(fingerprint, outcome)
        log.info("proposal %s approved by %s -> %s", fingerprint[:12], settings.reviewer_name, "applied" if result.ok else "failed")
        return RedirectResponse(f"/proposals/{fingerprint}?msg={'applied' if result.ok else 'failed'}", status_code=303)

    @app.post("/proposals/{fingerprint}/reject")
    def reject(request: Request, fingerprint: str, note: Annotated[str, Form()] = "", back: Annotated[str, Form()] = "") -> Response:
        if _foreign_origin(request):
            return refused()
        with lock:
            sp = store.get(fingerprint)
            if sp is None:
                return not_found(fingerprint)
            if sp.status not in APPROVABLE:
                return conflict(sp, "rejected")
            store.decide(fingerprint, "rejected", by=settings.reviewer_name, note=note.strip())
        log.info("proposal %s rejected by %s", fingerprint[:12], settings.reviewer_name)
        tab = back if back in TAB_KEYS else "pending"
        return RedirectResponse(f"/?status={tab}&msg=rejected&fp={quote(fingerprint)}", status_code=303)

    @app.post("/proposals/{fingerprint}/reopen")
    def reopen(request: Request, fingerprint: str) -> Response:
        if _foreign_origin(request):
            return refused()
        with lock:
            sp = store.get(fingerprint)
            if sp is None:
                return not_found(fingerprint)
            if sp.status not in REOPENABLE:
                return conflict(sp, "reopened")
            if isinstance(sp.apply_result, dict) and any(s.get("ok") for s in sp.apply_result.get("steps") or []):
                log.warning("reopening %s although some of its steps were already applied: %s", fingerprint[:12], sp.apply_result)
            try:
                store.reopen(fingerprint)
            except PartiallyAppliedError as exc:
                flash = {"level": "warn", "text": f"Not reopened: {exc}. Nothing was written to the CRM."}
                return render_detail(sp, flash=flash, status_code=409)
        return RedirectResponse(f"/proposals/{fingerprint}?msg=reopened", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs() -> HTMLResponse:
        with lock:
            rows = store.runs(limit=200)
        return render("runs.html", nav="runs", runs=rows)

    # ------------------------------------------------------------------ JSON
    @app.get("/api/proposals")
    def api_proposals(status: str = "", kind: str = "") -> list[dict[str, Any]]:
        with lock:
            return [sp.to_dict() for sp in store.list(status or None, kind or None)]

    @app.get("/api/proposals/{fingerprint}")
    def api_proposal(fingerprint: str) -> Response:
        with lock:
            sp = store.get(fingerprint)
        if sp is None:
            return JSONResponse({"detail": "proposal not found"}, status_code=404)
        return JSONResponse(sp.to_dict())

    @app.get("/health")
    def health() -> dict[str, Any]:
        with lock:
            counts = tab_counts(store.list())
            raw = store.counts()
        return {"ok": True, "pending": counts["pending"], "counts": raw}

    return app


__all__ = ["create_app", "Views", "money"]
