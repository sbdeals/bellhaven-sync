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
from .match import Candidate, LocationMatch, contacts_by_account, explain_survivor, is_parent_like, parent_ids_of
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
        self.stamp = f"[bellhaven-sync {today.isoformat()}]"
        self.parent_ids = parent_ids_of(accounts)

    # ------------------------------------------------------------- primitives
    def note(self, account: Account | None, text: str) -> str:
        new = f"{self.stamp} {text}"
        old = (account.note or "").strip() if account else ""
        return f"{old} | {new}" if old else new

    def patch(self, account: Account, fields: dict[str, Any], note: str, label: str) -> Action | None:
        """A patch with only the fields that would actually change; None if nothing would."""
        changes = {k: v for k, v in fields.items() if (getattr(account, k, "") or "") != (v or "")}
        if not changes:
            return None
        changes["note"] = self.note(account, note)
        return Action(op="patch", account_id=account.account_id, fields=changes, label=label)

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
        elif normalize_zip(a.billing_zip) != normalize_zip(site.zip):
            notes.append(f"CRM zip '{a.billing_zip}' differs from the website zip '{site.zip}' (not changed).")
        if site.phone and not s.phone_match:
            notes.append(f"CRM phone '{a.phone or '-'}' differs from the website phone '{site.phone}' (not changed).")
        mapped = map_care_types(site.care_offerings)
        if a.care_type and mapped and a.care_type not in mapped:
            notes.append(
                f"CRM care_type '{a.care_type}' vs website offerings {', '.join(site.care_offerings)} "
                f"(maps to {', '.join(mapped)}); not changed."
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
            "Duplicate copies at this address get their own 'duplicate' proposals: "
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


def _duplicate_proposals(b: _Builder, m: LocationMatch) -> list[Proposal]:
    site, surv = m.site, m.survivor
    assert surv is not None
    out = []
    for l in m.losers:
        a = l.account
        status = "Needs Review" if a.has_revenue_and_ar else "Inactive"
        why_status = (
            f"set to Needs Review (not Inactive) because it has revenue {_money(a.lifetime_revenue)} and open AR "
            f"{_money(a.outstanding_ar)}" if status == "Needs Review" else "set Inactive"
        )
        action = b.patch(
            a, {"duplicate_of_account": surv.account.account_id, "status": status},
            f"Duplicate of {surv.account.account_id} '{surv.account.name}' (same facility at {site.street}, "
            f"{site.city} on the Bellhaven website); {why_status}. Use the surviving account going forward.",
            label=f"Mark duplicate of {surv.account.account_id} ({status})",
        )
        if action is None:
            continue
        others = [(x.account, "loser") for x in m.losers if x is not l]
        notes = [f"Match: {m.rule}", *m.notes, m.survivor_rule]
        if status == "Needs Review":
            notes.append("Billing history stays visible: the account is not inactivated while AR is open (SOP).")
        out.append(
            Proposal(
                kind=ProposalKind.DUPLICATE,
                priority="high",
                title=f"Duplicate: '{a.name}' ({a.account_id}) -> {surv.account.account_id} '{surv.account.name}'",
                summary=(
                    f"{a.account_id} ({a.name}, under {b.parent_display(a)}) is the same facility as survivor "
                    f"{surv.account.account_id}; point it at the survivor and set it {status}."
                ),
                actions=[action],
                evidence=b.evidence(
                    site=site, accounts=[(a, "loser"), (surv.account, "survivor"), *others], score=l.score.to_dict(),
                    rule=(
                        f"Duplicate cluster at one website address: survivor chosen by ordering "
                        f"({explain_survivor(surv, l, b.bh)}), every other address match becomes a duplicate."
                    ),
                    notes=notes,
                ),
                confidence=round(l.score.total / 100.0, 2),
                subject_account_id=a.account_id,
                site_slug=site.slug,
            )
        )
    return out


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
        if p is not None:
            proposals.append(p)
            if p.kind == ProposalKind.CHOW_REPARENT and p.subject_account_id:
                chow_retained.add(p.subject_account_id)
        proposals.extend(_duplicate_proposals(b, m))

    proposals.extend(_crm_only_proposals(b, matched_ids))
    proposals.extend(_parent_acquired_proposals(b, matched_ids, chow_retained, match_roles))
    proposals.sort(key=lambda p: (PRIORITY_ORDER.get(p.priority, 9), str(p.kind), p.title, p.fingerprint))
    return proposals
