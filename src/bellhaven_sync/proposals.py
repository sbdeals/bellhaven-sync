"""Turn match results + current CRM state into reviewable `Proposal`s.

Pure: no I/O. Implements the "SOP", "Proposals and idempotency" and CRM-only sections of
the design spec. Every write carries a dated note for the sales team; a proposal whose
actions would not change anything in the CRM is never emitted.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Iterable

from rapidfuzz import fuzz

from .config import DEFAULT_PARENT_NAME
from .match import (
    SURVIVOR_CRITERIA,
    Candidate,
    LocationMatch,
    contacts_by_account,
    is_parent_like,
    parent_ids_of,
    survivor_sort_key,
)
from .models import (
    NEW_ACCOUNT_REF,
    PRIORITY_ORDER,
    Account,
    Action,
    Contact,
    Priority,
    Proposal,
    ProposalKind,
    SiteLocation,
)
from .normalize import (
    is_po_box,
    map_care_type,
    map_care_types,
    names_equal_strict,
    normalize_name,
    normalize_street,
    normalize_zip,
    significant_tokens,
)

log = logging.getLogger(__name__)

GLOBAL_NAME_COLLISION_RATIO = 85.0  # fuzz.ratio of loose names, for "possibly related" evidence
NAME_TIE_MARGIN = 5.0  # a survivor decided by a smaller name-similarity gap is reported as a tie
_PARENT_SUFFIX_RE = re.compile(r"\s*\(parent account\)\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------- parent


def resolve_parent_id(
    accounts: Iterable[Account], *, parent_id: str = "", parent_name: str = DEFAULT_PARENT_NAME
) -> str:
    """The Bellhaven parent account: `parent_id` if given (must exist), else the account
    whose name equals `parent_name`. Raises LookupError if missing or ambiguous."""
    accounts = list(accounts)
    if parent_id:
        if any(a.account_id == parent_id for a in accounts):
            return parent_id
        raise LookupError(f"parent account {parent_id!r} not found in the CRM")
    want = " ".join(parent_name.split()).casefold()
    hits = [a for a in accounts if " ".join(a.name.split()).casefold() == want]
    if len(hits) != 1:
        raise LookupError(f"expected exactly one account named {parent_name!r}, found {len(hits)}")
    return hits[0].account_id


def parent_label(account: Account) -> str:
    """'Harborview Care Group (Parent Account)' -> 'Harborview Care Group'."""
    return _PARENT_SUFFIX_RE.sub("", account.name).strip()


# --------------------------------------------------------------------------- helpers


def _money(x: float | str | None) -> str:
    try:
        return f"${float(x or 0):,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _addr(street: str, city: str, state: str, zip_code: str) -> str:
    return f"{street}, {city}, {state} {zip_code}".strip(", ")


def _site_addr(site: SiteLocation) -> str:
    return _addr(site.street, site.city, site.state, site.zip)


def _acct_addr(a: Account) -> str:
    return _addr(a.billing_street, a.billing_city, a.billing_state, a.billing_zip)


def _norm_text(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def _stamp(today: date) -> str:
    return f"[bellhaven-sync {today.isoformat()}]"


def _note_text(stamp: str, account: Account | None, text: str) -> str:
    new = f"{stamp} {text}"
    old = (account.note or "").strip() if account else ""
    return f"{old} | {new}" if old else new


def _patch_action(stamp: str, account: Account, fields: dict[str, Any], note: str, label: str) -> Action | None:
    """A patch with only the fields that would actually change; None if nothing would."""
    changes = {k: v for k, v in fields.items() if (getattr(account, k, "") or "") != (v or "")}
    if not changes:
        return None
    changes["note"] = _note_text(stamp, account, note)
    return Action(op="patch", account_id=account.account_id, fields=changes, label=label)


def _parent_label_of(a: Account) -> str:
    return _PARENT_SUFFIX_RE.sub("", a.parent_name or "").strip() or (a.parent_id or "no parent")


class _Builder:
    """Holds the run-wide context so the per-kind functions stay short."""

    def __init__(
        self,
        accounts: list[Account],
        contacts: list[Contact],
        bellhaven_parent_id: str,
        about_text: str | None,
        today: date,
    ) -> None:
        self.accounts = accounts
        self.by_id = {a.account_id: a for a in accounts}
        self.contacts = contacts_by_account(contacts)
        self.bh = bellhaven_parent_id
        self.bh_name = parent_label(self.by_id[bellhaven_parent_id]) if bellhaven_parent_id in self.by_id else "Bellhaven"
        self.about_text = about_text or ""
        self.today = today
        self.stamp = _stamp(today)
        self.parent_ids = parent_ids_of(accounts)

    # ------------------------------------------------------------- primitives
    def note(self, account: Account | None, text: str) -> str:
        return _note_text(self.stamp, account, text)

    def patch(self, account: Account, fields: dict[str, Any], note: str, label: str) -> Action | None:
        """A patch with only the fields that would actually change; None if nothing would."""
        return _patch_action(self.stamp, account, fields, note, label)

    def will_be_bellhaven(self, a: Account) -> bool:
        """Under Bellhaven now, or moved there by this run (reparent / duplicate survivor);
        False for the CHOW case, where a new account takes the website values instead."""
        return a.parent_id == self.bh or not a.has_revenue_and_ar

    def create(self, site: SiteLocation, note: str, label: str) -> Action:
        care = map_care_type(site.care_offerings[0]) if site.care_offerings else ""
        fields = {
            "name": site.name,
            "parent_id": self.bh,
            "billing_street": site.street,
            "billing_city": site.city,
            "billing_state": site.state,
            "billing_zip": site.zip,
            "care_type": care,
            "phone": site.phone,
            "status": "Active",
            "note": self.note(None, note),
        }
        return Action(op="create", account_id=None, fields=fields, label=label)

    def site_facts(self, site: SiteLocation) -> str:
        offerings = ", ".join(site.care_offerings) or "none listed"
        admin = f" Administrator: {site.administrator}." if site.administrator else ""
        return f"Care offerings: {offerings}.{admin} Source: {site.source_url}"

    def evidence(
        self,
        *,
        site: SiteLocation | None,
        accounts: list[tuple[Account, str]],
        score: dict[str, Any] | None,
        rule: str,
        notes: list[str],
        about_text: str | None = None,
    ) -> dict[str, Any]:
        seen: set[str] = set()
        rows = []
        for acct, role in accounts:
            if acct.account_id in seen:
                continue
            seen.add(acct.account_id)
            rows.append({**acct.to_dict(), "role": role})
        return {
            "site": site.to_dict() if site else None,
            "crm_accounts": rows,
            "contacts": {aid: [c.to_dict() for c in self.contacts.get(aid, [])] for aid in seen},
            "score": score,
            "rule": rule,
            "notes": notes,
            "about_text": about_text,
        }

    def parent_of(self, a: Account) -> Account | None:
        return self.by_id.get(a.parent_id) if a.parent_id else None

    def parent_display(self, a: Account) -> str:
        p = self.parent_of(a)
        return parent_label(p) if p else (a.parent_name or "no parent")

    # ---------------------------------------------------------- evidence notes
    def difference_notes(self, site: SiteLocation, c: Candidate) -> list[str]:
        a, s = c.account, c.score
        notes: list[str] = []
        if not s.street_match:
            if is_po_box(a.billing_street):
                notes.append(
                    f"CRM billing street '{a.billing_street}' differs from the website street '{site.street}'; "
                    "a PO Box billing address is legitimate, so no address change is proposed."
                )
            else:
                notes.append(
                    f"CRM billing street '{a.billing_street}' differs from the website street '{site.street}'; "
                    "matched on other evidence, address not changed (check manually)."
                )
        drift = "proposed separately as field_drift" if self.will_be_bellhaven(a) else "not changed"
        if s.street_match and normalize_zip(a.billing_zip) != normalize_zip(site.zip):
            notes.append(f"CRM zip '{a.billing_zip}' differs from the website zip '{site.zip}' ({drift}).")
        if site.phone and not s.phone_match:
            notes.append(f"CRM phone '{a.phone or '-'}' differs from the website phone '{site.phone}' (not changed).")
        mapped = map_care_types(site.care_offerings)
        if a.care_type and mapped and a.care_type not in mapped:
            notes.append(
                f"CRM care_type '{a.care_type}' vs website offerings {', '.join(site.care_offerings)} "
                f"(maps to {', '.join(mapped)}); {drift}."
            )
        if site.administrator and not s.admin_match:
            names = ", ".join(x.name for x in self.contacts.get(a.account_id, []) if x.is_active) or "no contacts"
            notes.append(f"Website administrator '{site.administrator}' is not a contact on this account ({names}).")
        if a.status != "Active":
            notes.append(f"Account status is '{a.status}' although the location is on the website (not changed).")
        if not site.listed_in_directory:
            notes.append("This location is only linked from the website homepage; it is not in the /communities directory.")
        return notes

    def why_not_matched(self, site: SiteLocation, c: Candidate) -> str:
        a, s = c.account, c.score
        street = "street matches" if s.street_match else f"street '{a.billing_street}' vs '{site.street}'"
        phone = "phone matches" if s.phone_match else f"phone {a.phone or '-'} vs {site.phone or '-'}"
        names = ", ".join(x.name for x in self.contacts.get(a.account_id, []) if x.is_active) or "no contacts"
        admin = (
            "administrator is a contact" if s.admin_match
            else f"administrator {site.administrator or '-'} not among its contacts ({names})"
        )
        return (
            f"{a.name} ({a.account_id}, under {self.parent_display(a)}) at {_acct_addr(a)} was not matched: "
            f"{street}; {phone}; {admin}; name similarity {s.name_similarity:g}. Listed as possibly related only."
        )

    def global_name_collisions(self, site: SiteLocation, exclude: set[str]) -> list[tuple[Account, float]]:
        target = normalize_name(site.name)
        out = []
        for a in self.accounts:
            if a.account_id in exclude or is_parent_like(a, self.parent_ids):
                continue
            r = round(float(fuzz.ratio(target, normalize_name(a.name))), 1)
            if r >= GLOBAL_NAME_COLLISION_RATIO:
                out.append((a, r))
        out.sort(key=lambda x: (-x[1], x[0].account_id))
        return out[:5]


# --------------------------------------------------------------------------- per-location


def _matched_proposal(b: _Builder, m: LocationMatch) -> Proposal | None:
    """confirmed / rename / reparent / chow_reparent for the (surviving) matched account."""
    site, c = m.site, m.survivor
    assert c is not None
    a = c.account
    score = c.score.to_dict()
    notes = [f"Match: {m.rule}", *m.notes, *b.difference_notes(site, c)]
    if m.is_duplicate_cluster:
        notes.append(m.survivor_rule)
        notes.append(
            "Duplicate copies at this address are handled by one 'duplicate' group proposal: "
            + ", ".join(f"{l.account.account_id} {l.account.name}" for l in m.losers) + "."
        )
    loser_rows = [(l.account, "loser") for l in m.losers]
    confidence = round(c.score.total / 100.0, 2)
    name_differs = not names_equal_strict(a.name, site.name)

    if a.parent_id == b.bh:
        if not name_differs:
            return Proposal(
                kind=ProposalKind.CONFIRMED,
                priority="low",
                title=f"Confirmed: {site.name}",
                summary=f"{a.name} ({a.account_id}) is under {b.bh_name} and matches the website. Nothing to change.",
                actions=[],
                evidence=b.evidence(
                    site=site, accounts=[(a, "subject"), *loser_rows], score=score,
                    rule="Matched account is already under Bellhaven with the website's exact name: informational only.",
                    notes=notes,
                ),
                confidence=confidence,
                subject_account_id=a.account_id,
                site_slug=site.slug,
            )
        cosmetic = normalize_name(a.name) == normalize_name(site.name)
        priority: Priority = "low" if cosmetic else "high"
        if cosmetic:
            why = "the difference is cosmetic (spelling/abbreviation/stopwords)"
        elif not (significant_tokens(a.name) & significant_tokens(site.name)):
            why = "the CRM name shares no distinctive word with the website name"
        else:
            why = "the names differ beyond spelling"
        action = b.patch(
            a, {"name": site.name},
            f"Renamed from '{a.name}' to '{site.name}' to match the Bellhaven website ({site.source_url}).",
            label=f"Rename to '{site.name}'",
        )
        if action is None:
            return None
        return Proposal(
            kind=ProposalKind.RENAME,
            priority=priority,
            title=f"Rename '{a.name}' to '{site.name}'",
            summary=f"{a.account_id} is the website's {site.name} (same facility) but the CRM name differs; {why}.",
            actions=[action],
            evidence=b.evidence(
                site=site, accounts=[(a, "subject"), *loser_rows], score=score,
                rule=f"Same facility under Bellhaven with a different name; {why}, so priority is {priority}.",
                notes=notes,
            ),
            confidence=confidence,
            subject_account_id=a.account_id,
            site_slug=site.slug,
        )

    old_parent = b.parent_of(a)
    parent_rows = [(old_parent, "parent")] if old_parent else []
    from_parent = parent_label(old_parent) if old_parent else "no parent"

    if a.has_revenue_and_ar:  # SOP: change of ownership, never move billing history
        create = b.create(
            site,
            f"New Bellhaven account after change of ownership (CHOW from {a.account_id} {a.name}, which stays under "
            f"{from_parent} with lifetime revenue {_money(a.lifetime_revenue)} and open AR {_money(a.outstanding_ar)}). "
            + b.site_facts(site),
            label=f"Create '{site.name}' under Bellhaven",
        )
        old = b.patch(
            a, {"chow_current_account": NEW_ACCOUNT_REF},
            f"Change of ownership: this facility is now Bellhaven's '{site.name}' (listed on the Bellhaven website). "
            f"This account keeps its billing history and open AR ({_money(a.outstanding_ar)}); new business goes to the "
            "account in chow_current_account. Parent, status and name intentionally unchanged (CHOW SOP).",
            label="Point old account at the new Bellhaven account (chow_current_account)",
        )
        assert old is not None  # the old account is not superseded, so the pointer is a change
        return Proposal(
            kind=ProposalKind.CHOW_REPARENT,
            priority="high",
            title=f"Change of ownership: {site.name} (new account; keep {a.account_id})",
            summary=(
                f"{a.name} ({a.account_id}) under {from_parent} is on the Bellhaven website but has revenue AND open AR, "
                "so per the SOP a new Bellhaven account is created and the old one points to it."
            ),
            actions=[create, old],
            evidence=b.evidence(
                site=site, accounts=[(a, "old"), *parent_rows, *loser_rows], score=score,
                rule="SOP: matched account is outside Bellhaven and has lifetime revenue > 0 AND outstanding AR > 0, "
                "so create a new Bellhaven account and set chow_current_account on the old one instead of reparenting.",
                notes=notes,
            ),
            confidence=confidence,
            subject_account_id=a.account_id,
            site_slug=site.slug,
        )

    fields: dict[str, Any] = {"parent_id": b.bh}
    if name_differs:
        fields["name"] = site.name
    renamed = f" and renamed from '{a.name}' to '{site.name}'" if name_differs else ""
    moved = f"Moved from {from_parent} to {b.bh_name}" if old_parent else f"Linked to parent {b.bh_name} (had no parent)"
    sop = "No open AR" if not (a.outstanding_ar or 0) > 0 else "No lifetime revenue"
    action = b.patch(
        a, fields,
        f"{moved}{renamed}: the facility is listed on the Bellhaven website ({site.source_url}). "
        f"{sop}, so the account moves directly (CHOW SOP not needed).",
        label="Move under Bellhaven" + (f" and rename to '{site.name}'" if name_differs else ""),
    )
    if action is None:
        return None
    return Proposal(
        kind=ProposalKind.REPARENT,
        priority="high",
        title=f"Move '{a.name}' to Bellhaven" + (f" as '{site.name}'" if name_differs else ""),
        summary=(
            f"{a.account_id} is the website's {site.name} but sits under {from_parent}; revenue "
            f"{_money(a.lifetime_revenue)}, AR {_money(a.outstanding_ar)}, so it moves to Bellhaven directly"
            + (f" and takes the website name '{site.name}'." if name_differs else ".")
        ),
        actions=[action],
        evidence=b.evidence(
            site=site, accounts=[(a, "subject"), *parent_rows, *loser_rows], score=score,
            rule="Matched account is outside Bellhaven without (revenue AND open AR), so parent_id moves to Bellhaven"
            + (" and the name takes the website spelling." if name_differs else "."),
            notes=notes,
        ),
        confidence=confidence,
        subject_account_id=a.account_id,
        site_slug=site.slug,
    )


def _rank_reason(hi: Candidate, lo: Candidate, bh: str) -> tuple[str, int]:
    """Short text naming the first survivor criterion that puts `hi` above `lo`, and its index."""
    kh, kl = survivor_sort_key(hi, bh), survivor_sort_key(lo, bh)
    for i, (x, y) in enumerate(zip(kh, kl, strict=True)):
        if x == y:
            continue
        crit = SURVIVOR_CRITERIA[i]
        if crit == "parent is Bellhaven":
            return "already under Bellhaven", i
        if crit == "lifetime revenue > 0":
            return f"has lifetime revenue ({_money(hi.account.lifetime_revenue)})", i
        if crit == "higher name similarity":
            return f"name similarity {hi.score.name_similarity:.1f} vs {lo.score.name_similarity:.1f}", i
        if crit == "more contacts":
            return f"more contacts ({hi.n_contacts} vs {lo.n_contacts})", i
        if crit == "lowest account_id":
            return "tie: lowest account id", i
        return crit, i  # "website phone matches" / "website administrator is a contact"
    return "tie: indistinguishable", len(kh) - 1


def _is_tie(hi: Candidate, lo: Candidate, bh: str) -> bool:
    """Nothing substantive separates the two: the ordering fell through to the account-id
    tiebreak, or was decided by a name-similarity gap below NAME_TIE_MARGIN."""
    _, i = _rank_reason(hi, lo, bh)
    crit = SURVIVOR_CRITERIA[i]
    if crit == "lowest account_id":
        return True
    return crit == "higher name similarity" and abs(hi.score.name_similarity - lo.score.name_similarity) < NAME_TIE_MARGIN


def build_duplicate_group_actions(
    evidence: dict[str, Any], survivor_id: str, *, today: date | None = None
) -> list[Action]:
    """Actions for a duplicate cluster with `survivor_id` as the surviving account.

    Pure; works from a `duplicate` proposal's evidence alone so the review app can rebuild the
    actions for the reviewer's chosen survivor. In order: (a) every other candidate points at
    the survivor and goes Inactive (Needs Review if it has revenue AND open AR); (b) the
    survivor takes the website name and moves under Bellhaven, unless it has revenue AND open
    AR outside Bellhaven, in which case only a note is written (CHOW SOP: the next run
    proposes a chow_reparent for it)."""
    cluster = evidence["cluster"]
    site = evidence["site"]
    bh = cluster["bellhaven_parent_id"]
    bh_name = cluster.get("bellhaven_parent_name") or "Bellhaven"
    rows = sorted(cluster["candidates"], key=lambda r: r.get("rank", 0))
    by_id = {r["account_id"]: r for r in rows}
    if survivor_id not in by_id:
        raise ValueError(f"{survivor_id!r} is not a candidate of this duplicate cluster ({', '.join(by_id)})")
    stamp = _stamp(today or date.today())
    surv = Account.from_api(by_id[survivor_id])
    where = f"{site['street']}, {site['city']}"
    actions: list[Action] = []

    for r in rows:
        if r["account_id"] == survivor_id:
            continue
        a = Account.from_api(r)
        status = "Needs Review" if a.has_revenue_and_ar else "Inactive"
        why_status = (
            f"set to Needs Review (not Inactive) because it has revenue {_money(a.lifetime_revenue)} and open AR "
            f"{_money(a.outstanding_ar)}" if status == "Needs Review" else "set Inactive"
        )
        act = _patch_action(
            stamp, a, {"duplicate_of_account": surv.account_id, "status": status},
            f"Duplicate of {surv.account_id} '{surv.name}' (same facility at {where} on the Bellhaven website); "
            f"{why_status}. Use the surviving account going forward.",
            label=f"Mark '{a.name}' ({a.account_id}) duplicate of {surv.account_id} ({status})",
        )
        if act is not None:
            actions.append(act)

    from_parent = _parent_label_of(surv) if surv.parent_id else "no parent"
    not_bh = surv.parent_id != bh
    name_differs = not names_equal_strict(surv.name, site["name"])
    if not_bh and surv.has_revenue_and_ar:
        actions.append(
            Action(
                op="patch",
                account_id=surv.account_id,
                fields={
                    "note": _note_text(
                        stamp, surv,
                        f"Kept as the surviving account of the duplicate cluster at {where}. It has lifetime revenue "
                        f"{_money(surv.lifetime_revenue)} and open AR {_money(surv.outstanding_ar)} under {from_parent}, "
                        "so its parent and name are not changed (CHOW SOP); the next sync run proposes a change of "
                        "ownership (chow_reparent) for it.",
                    )
                },
                label=f"Survivor stays under {from_parent} (CHOW SOP; note only)",
            )
        )
    elif not_bh or name_differs:
        fields: dict[str, Any] = {}
        parts: list[str] = []
        if not_bh:
            fields["parent_id"] = bh
            parts.append(f"moved from {from_parent} to {bh_name}" if surv.parent_id else f"linked to parent {bh_name}")
        if name_differs:
            fields["name"] = site["name"]
            parts.append(f"renamed from '{surv.name}' to '{site['name']}'")
        sop = ""
        if not_bh:
            sop = (" No open AR" if not (surv.outstanding_ar or 0) > 0 else " No lifetime revenue") + (
                ", so the account moves directly (CHOW SOP not needed)."
            )
        act = _patch_action(
            stamp, surv, fields,
            f"Kept as the surviving account of the duplicate cluster at {where}; {' and '.join(parts)} to match the "
            f"Bellhaven website ({site.get('source_url', '')}).{sop}",
            label="Survivor: " + " and ".join(
                (["move under Bellhaven"] if not_bh else []) + ([f"rename to '{site['name']}'"] if name_differs else [])
            ),
        )
        if act is not None:
            actions.append(act)
    return actions


def _duplicate_group_proposal(b: _Builder, m: LocationMatch) -> Proposal | None:
    """One proposal per duplicate cluster; the reviewer may pick another survivor in the app."""
    site, surv = m.site, m.survivor
    assert surv is not None and m.is_duplicate_cluster
    ranked = list(m.matched)  # already in survivor order
    reasons = [_rank_reason(ranked[0], ranked[1], b.bh)[0]] + [
        f"below #{k}: {_rank_reason(ranked[k - 1], ranked[k], b.bh)[0]}" for k in range(1, len(ranked))
    ]
    tie = _is_tie(ranked[0], ranked[1], b.bh)
    candidates = [
        {
            **c.account.to_dict(),
            "contacts": [x.to_dict() for x in b.contacts.get(c.account.account_id, [])],
            "phone_match": c.score.phone_match,
            "admin_match": c.score.admin_match,
            "name_similarity": c.score.name_similarity,
            "rank": k + 1,
            "rank_reason": reasons[k],
        }
        for k, c in enumerate(ranked)
    ]
    a = surv.account
    notes = [f"Match: {m.rule}", *m.notes, m.survivor_rule, *b.difference_notes(site, surv)]
    if tie:
        notes.append(
            f"Tie: nothing substantive separates #1 {a.account_id} and #2 {ranked[1].account.account_id} "
            f"({reasons[0]}); please choose the survivor deliberately."
        )
    for c in ranked[1:]:
        if c.account.has_revenue_and_ar:
            notes.append(
                f"{c.account.account_id} has revenue AND open AR: as a duplicate it goes to Needs Review, not Inactive (SOP)."
            )
    if a.parent_id != b.bh and a.has_revenue_and_ar:
        notes.append(
            f"Recommended survivor {a.account_id} has revenue AND open AR outside Bellhaven: its parent is not changed "
            "here; the next run proposes a chow_reparent for it."
        )
    evidence = b.evidence(
        site=site,
        accounts=[(a, "survivor"), *[(c.account, "loser") for c in ranked[1:]]],
        score=surv.score.to_dict(),
        rule=(
            f"Duplicate cluster: {len(ranked)} CRM accounts match one website address. Survivor ordering "
            f"({' > '.join(SURVIVOR_CRITERIA)}) recommends {a.account_id}; every other copy becomes its duplicate and "
            "the survivor takes the website name and Bellhaven parent. The reviewer may choose another survivor."
        ),
        notes=notes,
    )
    evidence["cluster"] = {
        "candidates": candidates,
        "recommended": a.account_id,
        "tie": tie,
        "bellhaven_parent_id": b.bh,
        "bellhaven_parent_name": b.bh_name,
    }
    actions = build_duplicate_group_actions(evidence, a.account_id, today=b.today)
    if not actions:
        return None
    return Proposal(
        kind=ProposalKind.DUPLICATE,
        priority="high",
        title=f"Duplicate accounts for '{site.name}': {len(ranked)} copies at {site.street}",
        summary=(
            f"{len(ranked)} CRM accounts are the website's {site.name} ({_site_addr(site)}). Recommended survivor: "
            f"{a.account_id} '{a.name}' ({reasons[0]})"
            + ("; this is a TIE, nothing substantive separates the top two" if tie else "")
            + (". The other copy points" if len(ranked) == 2 else f". The other {len(ranked) - 1} copies point")
            + " at the survivor and are retired; the survivor is aligned with the website."
        ),
        actions=actions,
        evidence=evidence,
        confidence=round(surv.score.total / 100.0, 2),
        subject_account_id=a.account_id,
        site_slug=site.slug,
    )


def _field_drift_proposal(b: _Builder, m: LocationMatch) -> Proposal | None:
    """billing_zip / care_type corrections for a matched account that is (or will be) under
    Bellhaven. Never phone or street (recorded in evidence notes only)."""
    site, c = m.site, m.survivor
    assert c is not None
    a = c.account
    if not b.will_be_bellhaven(a):
        return None  # CHOW: the new Bellhaven account is created with the website values
    fields: dict[str, Any] = {}
    drift: dict[str, Any] = {}
    changes: list[str] = []
    if c.score.street_match and normalize_zip(site.zip) and normalize_zip(a.billing_zip) != normalize_zip(site.zip):
        fields["billing_zip"] = site.zip
        drift["billing_zip"] = {"crm": a.billing_zip, "website": site.zip}
        changes.append(f"billing_zip '{a.billing_zip}' -> '{site.zip}'")
    mapped = map_care_types(site.care_offerings)
    if a.care_type and mapped and a.care_type not in mapped:
        fields["care_type"] = mapped[0]
        drift["care_type"] = {"crm": a.care_type, "website": list(site.care_offerings), "mapped": mapped}
        changes.append(f"care_type '{a.care_type}' -> '{mapped[0]}'")
    if not fields:
        return None
    action = b.patch(
        a, fields,
        f"Corrected from the Bellhaven website ({site.source_url}): {'; '.join(changes)}. "
        f"Website care offerings: {', '.join(site.care_offerings) or 'none listed'}.",
        label="Update " + " and ".join(fields) + " from the website",
    )
    if action is None:
        return None
    notes = [f"Match: {m.rule}", *(f"CRM {k} {v['crm']!r} vs website {v['website']!r}." for k, v in drift.items())]
    if site.phone and not c.score.phone_match:
        notes.append(f"CRM phone '{a.phone or '-'}' differs from the website phone '{site.phone}' (not changed).")
    evidence = b.evidence(
        site=site, accounts=[(a, "subject")], score=c.score.to_dict(),
        rule="Matched account is (or will be) under Bellhaven and the CRM billing_zip (same street) or care_type "
        "disagrees with the website: patch only those fields. Phone and street are never changed.",
        notes=notes,
    )
    evidence["drift"] = drift
    return Proposal(
        kind=ProposalKind.FIELD_DRIFT,
        priority="low",
        title=f"Field drift: '{a.name}' " + "; ".join(changes),
        summary=f"{a.account_id} matches the website's {site.name} but its CRM {' and '.join(drift)} disagrees with the website.",
        actions=[action],
        evidence=evidence,
        confidence=round(c.score.total / 100.0, 2),
        subject_account_id=a.account_id,
        site_slug=site.slug,
    )


def _create_proposal(b: _Builder, m: LocationMatch) -> Proposal:
    site = m.site
    rows: list[tuple[Account, str]] = []
    notes = [f"Match: {m.rule}", *m.notes]
    for c in m.related:
        rows.append((c.account, "related"))
        notes.append(b.why_not_matched(site, c))
    blocked = {c.account.account_id for c in m.candidates}
    for a, ratio in b.global_name_collisions(site, blocked):
        rows.append((a, "related"))
        notes.append(
            f"{a.name} ({a.account_id}) has a similar name (ratio {ratio:g}) but is in {a.billing_city}, "
            f"{a.billing_state} {a.billing_zip}, not {site.city}, {site.state}: different location, not a candidate."
        )
    if not site.listed_in_directory:
        notes.append("This location is only linked from the website homepage; it is not in the /communities directory.")
    action = b.create(
        site,
        f"Created from the Bellhaven website: {site.name}, {_site_addr(site)}. " + b.site_facts(site),
        label=f"Create '{site.name}' under Bellhaven",
    )
    return Proposal(
        kind=ProposalKind.CREATE,
        priority="high",
        title=f"Create '{site.name}'",
        summary=f"{site.name} ({_site_addr(site)}) is on the Bellhaven website but has no CRM account.",
        actions=[action],
        evidence=b.evidence(
            site=site, accounts=rows, score=None,
            rule="No CRM account matches this website location, so a new account is created under Bellhaven.",
            notes=notes,
        ),
        confidence=0.8 if rows else 0.9,
        subject_account_id=None,
        site_slug=site.slug,
    )


# --------------------------------------------------------------------------- CRM-only


def _crm_only_proposals(b: _Builder, matched_ids: set[str]) -> list[Proposal]:
    """Bellhaven children with no website match.

    chow_out: any non-superseded, non-Inactive child (Active OR Needs Review) whose address is
    shared by another operator's account gets the chow pointer; status/parent never touched.
    not_on_website: only Active children with no other owner; Needs Review/Inactive are left alone.
    """
    out = []
    for a in b.accounts:
        if (
            a.parent_id != b.bh or a.status == "Inactive" or a.is_superseded
            or is_parent_like(a, b.parent_ids) or a.account_id in matched_ids
        ):
            continue
        street, z = normalize_street(a.billing_street), normalize_zip(a.billing_zip)
        owners = sorted(
            (
                o for o in b.accounts
                if o.account_id != a.account_id and street and o.parent_id != b.bh and not o.is_superseded
                and o.status != "Inactive" and not is_parent_like(o, b.parent_ids)
                and normalize_street(o.billing_street) == street and normalize_zip(o.billing_zip) == z
            ),
            key=lambda o: (o.status != "Active", o.account_id),
        )
        if owners:
            o = owners[0]
            action = b.patch(
                a, {"chow_current_account": o.account_id},
                f"Change of ownership: no longer on the Bellhaven website; {o.name} ({o.account_id}, under "
                f"{b.parent_display(o)}) now operates at {a.billing_street}, {a.billing_city}. chow_current_account points "
                f"to it. Status and parent unchanged per the CHOW SOP (revenue {_money(a.lifetime_revenue)}, "
                f"open AR {_money(a.outstanding_ar)}).",
                label=f"Point at new owner {o.account_id} (chow_current_account)",
            )
            if action is None:
                continue
            out.append(
                Proposal(
                    kind=ProposalKind.CHOW_OUT,
                    priority="high",
                    title=f"Ownership changed: '{a.name}' -> '{o.name}'",
                    summary=(
                        f"{a.account_id} is under Bellhaven but not on the website, and {o.name} ({b.parent_display(o)}) "
                        "is at the same address: record the change of ownership."
                    ),
                    actions=[action],
                    evidence=b.evidence(
                        site=None, accounts=[(a, "subject"), (o, "new_owner")], score=None,
                        rule="Bellhaven account (Active or Needs Review) missing from the website whose normalised "
                        "street+zip is shared by another operator's account: chow_out (pointer only, per SOP).",
                        notes=[
                            f"Current status '{a.status}' is left as is.",
                            f"Same normalised address: '{a.billing_street}' / '{o.billing_street}', zip {a.billing_zip}.",
                            *(
                                [f"Other accounts at this address: {', '.join(x.account_id for x in owners[1:])}."]
                                if len(owners) > 1 else []
                            ),
                        ],
                    ),
                    confidence=0.9,
                    subject_account_id=a.account_id,
                    site_slug=None,
                )
            )
        elif a.status == "Active":
            action = b.patch(
                a, {"status": "Needs Review"},
                f"Not listed on the Bellhaven website and no other operator found at {a.billing_street}, "
                f"{a.billing_city}; please confirm whether this community closed, was sold or was renamed.",
                label="Set status to Needs Review",
            )
            if action is None:
                continue
            out.append(
                Proposal(
                    kind=ProposalKind.NOT_ON_WEBSITE,
                    priority="medium",
                    title=f"Not on website: '{a.name}'",
                    summary=f"{a.account_id} is an Active Bellhaven account but the website does not list it.",
                    actions=[action],
                    evidence=b.evidence(
                        site=None, accounts=[(a, "subject")], score=None,
                        rule="Active Bellhaven account with no website match and no other account at its address: "
                        "flag for review.",
                        notes=[
                            f"Revenue {_money(a.lifetime_revenue)}, open AR {_money(a.outstanding_ar)}.",
                            "No website location in the same state and city/zip matched this account.",
                        ],
                    ),
                    confidence=0.8,
                    subject_account_id=a.account_id,
                    site_slug=None,
                )
            )
    return out


def _about_sentence(about_text: str, label: str) -> str | None:
    want = _norm_text(label)
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(about_text.split())):
        if want and want in _norm_text(sentence):
            return sentence.strip()
    return None


def _parent_acquired_proposals(
    b: _Builder, matched_ids: set[str], chow_retained: set[str], match_roles: dict[str, str]
) -> list[Proposal]:
    if not b.about_text.strip():
        return []
    out = []
    for p in b.accounts:
        if p.account_id == b.bh or p.status != "Active" or not is_parent_like(p, b.parent_ids):
            continue
        label = parent_label(p)
        sentence = _about_sentence(b.about_text, label)
        if not sentence:
            continue
        children = [a for a in b.accounts if a.parent_id == p.account_id]
        blockers = []
        for c in children:
            if c.account_id in chow_retained:
                blockers.append(f"{c.account_id} stays under this parent by the CHOW SOP")
            elif c.account_id in matched_ids or c.duplicate_of_account or c.status != "Active":
                continue
            else:
                blockers.append(f"{c.account_id} {c.name} is Active and not on the Bellhaven website")
        linked = [c for c in children if c.account_id in matched_ids or c.duplicate_of_account]
        if blockers or not linked:
            log.debug("parent %s not acquired: %s", p.account_id, blockers or "no linked children")
            continue
        action = b.patch(
            p, {"status": "Inactive"},
            f"Acquired by Bellhaven per the website About page: \"{sentence}\" Every child account is now matched to a "
            "Bellhaven website location or marked duplicate.",
            label="Set acquired parent Inactive",
        )
        if action is None:
            continue
        rows = [(p, "subject")] + [(c, "related") for c in children]
        notes = []
        for c in children:
            how = (
                match_roles.get(c.account_id, "matched to a website location this run") if c.account_id in matched_ids
                else f"duplicate of {c.duplicate_of_account}" if c.duplicate_of_account
                else f"status {c.status}"
            )
            notes.append(f"Child {c.account_id} {c.name}: {how}.")
        out.append(
            Proposal(
                kind=ProposalKind.PARENT_ACQUIRED,
                priority="low",
                title=f"Acquired parent: '{p.name}' -> Inactive",
                summary=f"The About page says Bellhaven took over {label}, and all {len(children)} of its accounts are reconciled.",
                actions=[action],
                evidence=b.evidence(
                    site=None, accounts=rows, score=None,
                    rule="Parent account named on the About page as joining Bellhaven, with every Active child matched "
                    "or duplicated and none retained by the CHOW SOP: propose Inactive (reviewer decides).",
                    notes=notes,
                    about_text=sentence,
                ),
                confidence=0.6,
                subject_account_id=p.account_id,
                site_slug=None,
            )
        )
    return out


# --------------------------------------------------------------------------- entry point


def build_proposals(
    matches: Iterable[LocationMatch],
    accounts: Iterable[Account],
    contacts: Iterable[Contact],
    bellhaven_parent_id: str,
    *,
    about_text: str | None = None,
    today: date | None = None,
) -> list[Proposal]:
    """All proposals for one run, sorted by priority, kind, title."""
    matches = list(matches)
    b = _Builder(list(accounts), list(contacts), bellhaven_parent_id, about_text, today or date.today())

    proposals: list[Proposal] = []
    matched_ids: set[str] = set()
    match_roles: dict[str, str] = {}
    chow_retained: set[str] = set()
    for m in matches:
        if not m.matched:
            proposals.append(_create_proposal(b, m))
            continue
        matched_ids.update(m.account_ids)
        assert m.survivor is not None
        match_roles[m.survivor.account.account_id] = f"matches website location '{m.site.slug}'"
        for l in m.losers:
            match_roles[l.account.account_id] = (
                f"duplicate of {m.survivor.account.account_id} at website location '{m.site.slug}'"
            )
        p = _matched_proposal(b, m)
        if m.is_duplicate_cluster:
            surv = m.survivor.account
            if surv.parent_id != b.bh and surv.has_revenue_and_ar:
                chow_retained.add(surv.account_id)  # proposed as chow_reparent once the group is applied
            if p is not None and p.kind != ProposalKind.CONFIRMED:
                p = None  # the survivor's rename/reparent lives inside the duplicate group proposal
            g = _duplicate_group_proposal(b, m)
            if g is not None:
                proposals.append(g)
        if p is not None:
            proposals.append(p)
            if p.kind == ProposalKind.CHOW_REPARENT and p.subject_account_id:
                chow_retained.add(p.subject_account_id)
        d = _field_drift_proposal(b, m)
        if d is not None:
            proposals.append(d)

    proposals.extend(_crm_only_proposals(b, matched_ids))
    proposals.extend(_parent_acquired_proposals(b, matched_ids, chow_retained, match_roles))
    proposals.sort(key=lambda p: (PRIORITY_ORDER.get(p.priority, 9), str(p.kind), p.title, p.fingerprint))
    return proposals
