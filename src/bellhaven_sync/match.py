"""Deterministic matching of website locations to CRM accounts.

Pure functions: website locations + CRM accounts + contacts in, `LocationMatch` results out.
See the "Matching" section of the design spec for the rules implemented here.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from rapidfuzz import fuzz

from .models import Account, Contact, SiteLocation
from .normalize import (
    normalize_city,
    normalize_name,
    normalize_person,
    normalize_phone,
    normalize_state,
    normalize_street,
    normalize_zip,
)

log = logging.getLogger(__name__)

# Score weights (0..100 after capping).
W_STREET = 45
W_ZIP = 10
W_CITY = 10
W_PHONE = 20
W_ADMIN = 15
W_NAME = 25  # name similarity 0..100 scaled to 0..25
W_LOCALITY = 10

MATCH_THRESHOLD = 60.0
NAME_FALLBACK_SIMILARITY = 90.0
RELATED_NAME_SIMILARITY = 60.0

SURVIVOR_CRITERIA: tuple[str, ...] = (
    "parent is Bellhaven",
    "lifetime revenue > 0",
    "website phone matches",
    "website administrator is a contact",
    "higher name similarity",
    "more contacts",
    "lowest account_id",
)


# --------------------------------------------------------------------------- data


@dataclass
class Score:
    total: float
    components: dict[str, float]
    street_match: bool
    zip_match: bool
    city_match: bool
    phone_match: bool
    admin_match: bool
    name_similarity: float
    address_differs: bool
    raw_total: float = 0.0  # uncapped sum, only used to break ties between locations

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "components": dict(self.components),
            "street_match": self.street_match,
            "zip_match": self.zip_match,
            "city_match": self.city_match,
            "phone_match": self.phone_match,
            "admin_match": self.admin_match,
            "name_similarity": self.name_similarity,
            "address_differs": self.address_differs,
        }


@dataclass
class Candidate:
    account: Account
    score: Score
    n_contacts: int = 0


@dataclass
class LocationMatch:
    site: SiteLocation
    candidates: list[Candidate]          # every blocked candidate, best score first
    matched: list[Candidate]             # survivor first; empty -> no match
    rule: str                            # which decision rule fired
    related: list[Candidate] = field(default_factory=list)  # unmatched but worth showing
    survivor_rule: str = ""              # why the survivor won (clusters only)
    notes: list[str] = field(default_factory=list)

    @property
    def survivor(self) -> Candidate | None:
        return self.matched[0] if self.matched else None

    @property
    def losers(self) -> list[Candidate]:
        return self.matched[1:]

    @property
    def is_duplicate_cluster(self) -> bool:
        return len(self.matched) > 1

    @property
    def account_ids(self) -> list[str]:
        return [c.account.account_id for c in self.matched]


# --------------------------------------------------------------------------- helpers


def contacts_by_account(contacts: Iterable[Contact]) -> dict[str, list[Contact]]:
    out: dict[str, list[Contact]] = defaultdict(list)
    for c in contacts:
        out[c.account_id].append(c)
    return dict(out)


def parent_ids_of(accounts: Iterable[Account]) -> set[str]:
    return {a.parent_id for a in accounts if a.parent_id}


def is_parent_like(account: Account, parent_ids: set[str]) -> bool:
    """A corporate parent: referenced as someone's parent, or named "(Parent Account)" with
    no address, or has no billing address at all."""
    return (
        account.account_id in parent_ids
        or account.is_parent_account
        or (not account.billing_street and not account.billing_city)
    )


def eligible_accounts(accounts: Iterable[Account]) -> list[Account]:
    """Accounts that can be matched to a website location: not parents, not superseded."""
    accounts = list(accounts)
    pids = parent_ids_of(accounts)
    return [a for a in accounts if not is_parent_like(a, pids) and not a.is_superseded]


def block_candidates(site: SiteLocation, accounts: Iterable[Account]) -> list[Account]:
    """Same state AND (same zip OR same normalised city)."""
    st = normalize_state(site.state)
    z = normalize_zip(site.zip)
    city = normalize_city(site.city)
    out = []
    for a in accounts:
        if normalize_state(a.billing_state) != st or not st:
            continue
        if (z and normalize_zip(a.billing_zip) == z) or (city and normalize_city(a.billing_city) == city):
            out.append(a)
    return out


def name_similarity(a: str, b: str) -> float:
    """rapidfuzz token_set_ratio of loose canonical names, rounded to 0.1 so float noise
    never decides a survivor."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return round(float(fuzz.token_set_ratio(na, nb)), 1)


def _contains_locality(account_name: str, city: str) -> bool:
    c = normalize_city(city)
    return bool(c) and f" {c} " in f" {normalize_name(account_name)} "


def admin_matches(site: SiteLocation, contacts: Iterable[Contact]) -> bool:
    admin = normalize_person(site.administrator)
    if not admin:
        return False
    return any(c.is_active and normalize_person(c.name) == admin for c in contacts)


def score_account(site: SiteLocation, account: Account, contacts: Iterable[Contact]) -> Score:
    contacts = list(contacts)
    s_street, a_street = normalize_street(site.street), normalize_street(account.billing_street)
    street_match = bool(s_street) and s_street == a_street
    zip_match = bool(normalize_zip(site.zip)) and normalize_zip(site.zip) == normalize_zip(account.billing_zip)
    city_match = bool(normalize_city(site.city)) and normalize_city(site.city) == normalize_city(account.billing_city)
    s_phone = normalize_phone(site.phone)
    phone_match = len(s_phone) == 10 and s_phone == normalize_phone(account.phone)
    admin_match = admin_matches(site, contacts)
    sim = name_similarity(site.name, account.name)
    locality = _contains_locality(account.name, site.city)

    components: dict[str, float] = {
        "street": W_STREET if street_match else 0,
        "zip": W_ZIP if zip_match else 0,
        "city": W_CITY if city_match else 0,
        "phone": W_PHONE if phone_match else 0,
        "admin": W_ADMIN if admin_match else 0,
        "name": round(sim * W_NAME / 100.0, 2),
        "locality": W_LOCALITY if locality else 0,
    }
    raw = round(sum(components.values()), 2)
    return Score(
        total=min(100.0, raw),
        components=components,
        street_match=street_match,
        zip_match=zip_match,
        city_match=city_match,
        phone_match=phone_match,
        admin_match=admin_match,
        name_similarity=sim,
        address_differs=not street_match,
        raw_total=raw,
    )


def _conflicting(site: SiteLocation, account: Account, score: Score) -> bool:
    """Street AND phone both present and different, and the administrator is not a contact:
    the CRM record describes a different facility whatever its name says (Union Square)."""
    street_differs = bool(site.street and account.billing_street) and not score.street_match
    phone_differs = bool(normalize_phone(site.phone) and normalize_phone(account.phone)) and not score.phone_match
    return street_differs and phone_differs and not score.admin_match


def survivor_sort_key(c: Candidate, bellhaven_parent_id: str) -> tuple:
    """Sort ascending; the first candidate is the duplicate-cluster survivor."""
    a, s = c.account, c.score
    return (
        0 if a.parent_id == bellhaven_parent_id else 1,
        0 if (a.lifetime_revenue or 0) > 0 else 1,
        0 if s.phone_match else 1,
        0 if s.admin_match else 1,
        -s.name_similarity,
        -c.n_contacts,
        a.account_id,
    )


def explain_survivor(survivor: Candidate, loser: Candidate, bellhaven_parent_id: str) -> str:
    """Name the first survivor-ordering criterion that separates the two."""
    ks, kl = survivor_sort_key(survivor, bellhaven_parent_id), survivor_sort_key(loser, bellhaven_parent_id)
    for i, (x, y) in enumerate(zip(ks, kl, strict=True)):
        if x != y:
            crit = SURVIVOR_CRITERIA[i]
            if crit == "higher name similarity":
                detail = f" ({survivor.score.name_similarity:g} vs {loser.score.name_similarity:g})"
            elif crit == "more contacts":
                detail = f" ({survivor.n_contacts} vs {loser.n_contacts})"
            elif crit == "lowest account_id":
                detail = f" ({survivor.account.account_id} < {loser.account.account_id})"
            else:
                detail = ""
            return f"{survivor.account.account_id} beats {loser.account.account_id} on '{crit}'{detail}"
    return f"{survivor.account.account_id} and {loser.account.account_id} are indistinguishable"


# --------------------------------------------------------------------------- decisions


def match_location(
    site: SiteLocation,
    accounts: Iterable[Account],
    contacts: dict[str, list[Contact]],
    bellhaven_parent_id: str,
) -> LocationMatch:
    """Decide which (eligible) accounts are this website location. `accounts` must already
    exclude parents and superseded accounts (see `eligible_accounts`)."""
    cands = [
        Candidate(a, score_account(site, a, contacts.get(a.account_id, [])), len(contacts.get(a.account_id, [])))
        for a in block_candidates(site, accounts)
    ]
    cands.sort(key=lambda c: (-c.score.raw_total, c.account.account_id))

    primary = [
        c for c in cands
        if (c.score.street_match or (c.score.phone_match and c.score.admin_match)) and c.score.total >= MATCH_THRESHOLD
    ]
    if primary:
        matched = primary
        if all(c.score.street_match for c in primary):
            rule = f"Street-address match (normalised street, same state and city/zip) with score >= {MATCH_THRESHOLD:g}."
        else:
            rule = (
                f"Matched on phone AND administrator contact with score >= {MATCH_THRESHOLD:g}; the CRM billing "
                "street differs from the website street (recorded, not changed)."
            )
    else:
        matched = [
            c for c in cands
            if c.score.name_similarity >= NAME_FALLBACK_SIMILARITY
            and c.score.city_match
            and not _conflicting(site, c.account, c.score)
        ]
        if matched:
            rule = (
                f"Name similarity >= {NAME_FALLBACK_SIMILARITY:g} in the same city with no conflicting street+phone+"
                "administrator evidence; the street differs (recorded, not changed)."
            )
        else:
            rule = (
                "No match: no account in the same state and city/zip shares the street, or the phone and "
                "administrator, or a near-identical name without conflicting evidence."
            )

    matched.sort(key=lambda c: survivor_sort_key(c, bellhaven_parent_id))
    matched_ids = {c.account.account_id for c in matched}
    related = [
        c for c in cands
        if c.account.account_id not in matched_ids
        and (
            c.score.name_similarity >= RELATED_NAME_SIMILARITY
            or c.score.street_match or c.score.phone_match or c.score.admin_match
        )
    ]

    survivor_rule = ""
    if len(matched) > 1:
        survivor = matched[0]
        reasons = "; ".join(explain_survivor(survivor, l, bellhaven_parent_id) for l in matched[1:])
        survivor_rule = (
            f"Survivor ordering ({' > '.join(SURVIVOR_CRITERIA)}) keeps {survivor.account.account_id} "
            f"\"{survivor.account.name}\": {reasons}."
        )
    return LocationMatch(site=site, candidates=cands, matched=matched, rule=rule, related=related, survivor_rule=survivor_rule)


def _claim_rank(m: LocationMatch, c: Candidate) -> tuple:
    s = c.score
    return (-s.raw_total, not s.street_match, not s.phone_match, not s.admin_match, -s.name_similarity, m.site.slug)


def match_all(
    sites: Iterable[SiteLocation],
    accounts: Iterable[Account],
    contacts: Iterable[Contact],
    bellhaven_parent_id: str,
) -> list[LocationMatch]:
    """Match every website location. An account can belong to only one location: if two
    locations claim it, the better-scoring one keeps it and the other is re-decided
    without it."""
    sites = list(sites)
    eligible = eligible_accounts(accounts)
    cmap = contacts_by_account(contacts)
    excluded: dict[str, set[str]] = defaultdict(set)
    reasons: dict[str, list[str]] = defaultdict(list)

    results: dict[str, LocationMatch] = {}
    for _ in range(len(sites) + 1):
        results = {
            s.slug: match_location(s, [a for a in eligible if a.account_id not in excluded[s.slug]], cmap, bellhaven_parent_id)
            for s in sites
        }
        claims: dict[str, list[tuple[LocationMatch, Candidate]]] = defaultdict(list)
        for m in results.values():
            for c in m.matched:
                claims[c.account.account_id].append((m, c))
        changed = False
        for aid, lst in claims.items():
            if len(lst) < 2:
                continue
            lst.sort(key=lambda mc: _claim_rank(*mc))
            winner = lst[0][0].site.slug
            for m, _c in lst[1:]:
                excluded[m.site.slug].add(aid)
                reasons[m.site.slug].append(
                    f"Account {aid} also matches website location '{winner}' with a better score and was left to it."
                )
                changed = True
        if not changed:
            break

    out = []
    for s in sites:
        m = results[s.slug]
        m.notes.extend(reasons.get(s.slug, []))
        out.append(m)
    n_match = sum(1 for m in out if m.matched)
    n_clusters = sum(1 for m in out if m.is_duplicate_cluster)
    log.info("matched %d/%d website locations (%d duplicate clusters)", n_match, len(out), n_clusters)
    return out
