"""Pure normalisation helpers used by the matcher and the proposal builder.

Nothing here knows about accounts or proposals: strings in, strings out.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- names

NAME_STOPWORDS: frozenset[str] = frozenset({"the", "of", "at"})

# Whole-token rewrites applied after lowercasing and `&` -> `and`.
_NAME_TOKEN_MAP: dict[str, str] = {
    "rehab": "rehabilitation",
    "centre": "center",
    "ctr": "center",
}

# Words that say what kind of place it is rather than which place it is. Used only to
# explain renames ("shares no distinctive word"), never to decide a match.
GENERIC_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "bellhaven", "and", "care", "center", "senior", "living", "home", "retirement",
        "nursing", "rehabilitation", "healthcare", "health", "community", "communities",
        "campus", "assisted", "memory", "skilled",
    }
)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def name_tokens(name: str) -> list[str]:
    """Loose canonical tokens of a facility name (see `normalize_name`)."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"\bhealth\s+care\b", "healthcare", s)
    s = _PUNCT_RE.sub(" ", s.replace("_", " "))
    out: list[str] = []
    for tok in s.split():
        tok = _NAME_TOKEN_MAP.get(tok, tok)
        if tok in NAME_STOPWORDS:
            continue
        out.append(tok)
    return out


def normalize_name(name: str) -> str:
    """Loose canonical form used for MATCHING: lowercase, `&`->and, rehab->rehabilitation,
    centre->center, health care->healthcare, punctuation and the/of/at dropped.

    Two names with equal loose forms differ only cosmetically."""
    return " ".join(name_tokens(name))


def significant_tokens(name: str) -> set[str]:
    """Distinctive words of a name (brand and generic facility words removed)."""
    return {t for t in name_tokens(name) if t not in GENERIC_NAME_TOKENS and not t.isdigit()}


def names_equal_strict(a: str, b: str) -> bool:
    """Exact equality apart from surrounding / repeated whitespace. A CRM name that fails
    this but passes `normalize_name` equality still gets a (low priority) rename so the CRM
    carries the website's exact spelling."""
    return _collapse(a or "") == _collapse(b or "")


# --------------------------------------------------------------------------- streets

# USPS-style short forms (suffixes and directionals).
_STREET_TOKEN_MAP: dict[str, str] = {
    "street": "st", "str": "st",
    "avenue": "ave", "av": "ave", "avn": "ave",
    "road": "rd",
    "drive": "dr", "drv": "dr",
    "lane": "ln",
    "boulevard": "blvd", "blv": "blvd",
    "pike": "pk", "pke": "pk",
    "court": "ct",
    "place": "pl",
    "parkway": "pkwy", "pky": "pkwy",
    "highway": "hwy",
    "circle": "cir",
    "terrace": "ter",
    "trail": "trl",
    "square": "sq",
    "way": "way",
    "suite": "ste",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "saint": "st",
}

_PO_BOX_RE = re.compile(r"^\s*p\.?\s*o\.?\s*box\b|^\s*post\s+office\s+box\b", re.IGNORECASE)


def normalize_street(street: str) -> str:
    s = (street or "").lower().replace("#", " ")
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(_STREET_TOKEN_MAP.get(t, t) for t in s.split())


def is_po_box(street: str) -> bool:
    return bool(_PO_BOX_RE.search(street or ""))


# --------------------------------------------------------------------------- misc fields


def normalize_phone(phone: str) -> str:
    """Digits only, last 10 (drops a leading country code)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:]


def normalize_city(city: str) -> str:
    s = _PUNCT_RE.sub(" ", (city or "").lower())
    toks = ["st" if t == "saint" else t for t in s.split()]
    return " ".join(toks)


def normalize_state(state: str) -> str:
    return (state or "").strip().upper()


def normalize_zip(zip_code: str) -> str:
    digits = re.sub(r"\D", "", zip_code or "")
    return digits[:5]


_PERSON_TITLES = frozenset({"dr", "mr", "mrs", "ms", "miss", "rn", "lpn", "lnha", "md", "jr", "sr"})


def normalize_person(name: str) -> str:
    s = _PUNCT_RE.sub(" ", (name or "").lower())
    return " ".join(t for t in s.split() if t not in _PERSON_TITLES)


# --------------------------------------------------------------------------- care types

CARE_TYPE_MAP: dict[str, str] = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short-term rehabilitation and nursing": "Skilled Nursing",
    "memory support": "Memory Care",
    "assisted living": "Assisted Living",
    "independent living": "Independent Living",
}


def map_care_type(badge: str) -> str:
    """Website care badge -> CRM care_type vocabulary. Unknown badges pass through."""
    key = _collapse((badge or "").lower())
    return CARE_TYPE_MAP.get(key, _collapse(badge or ""))


def map_care_types(badges: tuple[str, ...] | list[str]) -> list[str]:
    return [map_care_type(b) for b in badges]
