from datetime import date

import pytest

from packetpilot.normalize import (
    fuzzy_contains,
    normalize,
    parse_bool,
    parse_date,
    parse_entity,
    parse_fein,
    parse_int,
    parse_lines,
    parse_money,
    parse_phone,
    values_equal,
)


@pytest.mark.parametrize("raw,expected", [
    ("$1,200,000", 1_200_000), ("USD 1,250,000", 1_250_000), ("$1.2M", 1_200_000), ("1.2 million", 1_200_000),
    ("$650K", 650_000), ("650,000.00", 650_000), ("$0.00", 0), ("about $2,100,000.", 2_100_000), ("TBD", None), (None, None),
])
def test_money(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("03/01/2027", date(2027, 3, 1)), ("2027-03-01", date(2027, 3, 1)), ("March 1, 2027", date(2027, 3, 1)),
    ("Mar. 15, 2027", date(2027, 3, 15)), ("1 Sep 2026", date(2026, 9, 1)), ("Sept 3, 2026", date(2026, 9, 3)),
    ("effective on 12/31/26 please", date(2026, 12, 31)), ("soon", None),
])
def test_dates(raw, expected):
    assert parse_date(raw) == expected


def test_ints_and_words():
    assert parse_int("18 employees") == 18
    assert parse_int("twenty-two") == 22
    assert parse_int("eleven") == 11
    assert parse_int("none") is None


def test_fein_keeps_invalid_as_written():
    assert parse_fein("123456789") == "12-3456789"
    assert parse_fein("12-345678") == "12-345678"  # left for the validity rule to flag
    assert parse_fein("") is None


def test_lines_checkboxes_and_lists():
    assert parse_lines("[X] General Liability   [ ] Commercial Auto   [X] Workers Compensation   [ ] Commercial Property") == ["GL", "WC"]
    assert parse_lines("General Liability, Commercial Auto") == ["AUTO", "GL"]
    assert parse_lines("[ ] General Liability [ ] Commercial Auto") is None


def test_bool_checkboxes():
    assert parse_bool("[X] Yes   [ ] No") is True
    assert parse_bool("[ ] Yes   [X] No") is False
    assert parse_bool("No") is False
    assert parse_bool("maybe") is None


def test_entity_and_phone():
    assert parse_entity("Limited Liability Company") == "LLC"
    assert parse_entity("General Partnership") == "Partnership"
    assert parse_entity("Sole Proprietorship") == "Sole Proprietor"
    assert parse_entity("Corporation") == "Corporation"
    assert parse_phone("614.555.0123") == "(614) 555-0123"
    assert parse_phone("555-0123") is None


def test_text_cleanup_strips_scan_debris():
    assert normalize("text", "___ Grace Moreau") == "Grace Moreau"
    assert normalize("text", "Pinecrest Electric Inc.") == "Pinecrest Electric Inc."


def test_values_equal():
    assert values_equal("money", 1_200_000, 1_210_000, 0.02)
    assert not values_equal("money", 1_200_000, 2_100_000, 0.02)
    assert values_equal("text", "Ridgeline Landscaping, L.L.C.", "Ridgeline Landscaping LLC", path="applicant.legal_name")
    assert not values_equal("text", "Ridgeline Landscaping LLC", "Ridgeway Landscaping LLC", path="applicant.legal_name")
    assert values_equal("lines", ["GL", "WC"], ["WC", "GL"])


def test_fuzzy_contains():
    assert fuzzy_contains("Annual Revenue:   $1,200,000\nNext", "annual revenue: $1,200,000")
    assert fuzzy_contains("Contact Phone: (303) 555-0188", "Contact Phone: (308) 555-0188", threshold=0.9)
    assert not fuzzy_contains("Annual Revenue: $1,200,000", "Annual Revenue: $9,999,999", threshold=0.97)
