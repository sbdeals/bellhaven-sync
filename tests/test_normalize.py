"""Normalisation: names, streets, phones, cities, people, care types."""

from __future__ import annotations

import pytest

from bellhaven_sync.normalize import (
    is_po_box,
    map_care_type,
    names_equal_strict,
    normalize_city,
    normalize_name,
    normalize_person,
    normalize_phone,
    normalize_street,
    normalize_zip,
    significant_tokens,
)


@pytest.mark.parametrize(
    ("crm", "site"),
    [
        ("Bellhaven Rehab and Nursing of Grove City", "Bellhaven Rehabilitation & Nursing of Grove City"),
        ("Bellhaven of Sycamore Ridge", "Bellhaven at Sycamore Ridge"),
        ("Arbors at Bellhaven Dayton", "The Arbors at Bellhaven - Dayton"),
        ("Bellhaven Health Care Center of Ashland", "Bellhaven Healthcare Centre of Ashland"),
        ("Kettering Care Centre", "kettering care center"),
    ],
)
def test_loose_name_equal_for_cosmetic_variants(crm: str, site: str) -> None:
    assert normalize_name(crm) == normalize_name(site)


@pytest.mark.parametrize(
    ("crm", "site"),
    [
        ("Riverbend Manor Care Center", "Bellhaven of Chagrin Falls"),
        ("Sunny Acres Retirement Home", "Bellhaven Willow Creek"),
        ("Chesterton Senior Commons", "Bellhaven of Chesterton"),
        ("Cedar Trail of Zanesville", "Bellhaven of Zanesville"),
        ("Bellhaven of New Carlisle", "Bellhaven of Carlisle"),
    ],
)
def test_loose_name_differs_for_real_renames(crm: str, site: str) -> None:
    assert normalize_name(crm) != normalize_name(site)


def test_normalize_name_shape() -> None:
    assert normalize_name("The Arbors at Bellhaven - Dayton") == "arbors bellhaven dayton"
    assert normalize_name("Bellhaven Rehab & Nursing") == "bellhaven rehabilitation and nursing"
    assert normalize_name("") == ""


def test_significant_tokens_drop_brand_and_generic_words() -> None:
    assert significant_tokens("Bellhaven of Chesterton") == {"chesterton"}
    assert significant_tokens("Riverbend Manor Care Center") == {"riverbend", "manor"}


def test_names_equal_strict_only_ignores_whitespace() -> None:
    assert names_equal_strict("Bellhaven of  Goshen ", "Bellhaven of Goshen")
    assert not names_equal_strict("Bellhaven of Sycamore Ridge", "Bellhaven at Sycamore Ridge")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1610 College Avenue", "1610 College Ave"),
        ("199 Barks Road West", "199 Barks Rd W"),
        ("4850 Northwest Sylvania Avenue", "4850 NW Sylvania Ave"),
        ("3313 Wilmington Pk", "3313 Wilmington Pike"),
        ("1250 Northwest Franklin St", "1250 NW Franklin Street"),
        ("1125 Logan Boulevard", "1125 Logan Blvd"),
        ("210 Orchard Ln", "210 Orchard Lane"),
        ("1120 West Main Street", "1120 W Main St"),
        ("805 Colegate Drive", "805 Colegate Dr."),
        ("45 St Lawrence Dr", "45 St. Lawrence Drive"),
    ],
)
def test_street_variants_normalise_equal(a: str, b: str) -> None:
    assert normalize_street(a) == normalize_street(b)


def test_street_differences_survive() -> None:
    assert normalize_street("240 Market St") != normalize_street("118 Union Square Dr")
    assert normalize_street("875 Elm St") == "875 elm st"


def test_po_box() -> None:
    assert is_po_box("PO Box 517")
    assert is_po_box("P.O. Box 12")
    assert not is_po_box("3156 W Prospect Rd")


def test_phone_digits_last_ten() -> None:
    assert normalize_phone("(734) 503-1363") == "7345031363"
    assert normalize_phone("+1 734.503.1363") == "7345031363"
    assert normalize_phone("") == ""


def test_city_zip_person() -> None:
    assert normalize_city("  Chagrin  Falls ") == "chagrin falls"
    assert normalize_zip("45429-1234") == "45429"
    assert normalize_person("Gloria  Lambert") == normalize_person("gloria lambert")
    assert normalize_person("Dr. Gloria Lambert") == "gloria lambert"


@pytest.mark.parametrize(
    ("badge", "care"),
    [
        ("Short-Term Rehabilitation & Nursing", "Skilled Nursing"),
        ("Memory Support", "Memory Care"),
        ("Assisted Living", "Assisted Living"),
        ("assisted living", "Assisted Living"),
        ("Respite Care", "Respite Care"),  # unknown badges pass through unchanged
    ],
)
def test_care_type_map(badge: str, care: str) -> None:
    assert map_care_type(badge) == care
