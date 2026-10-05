"""llm/verify.py: money detection and the prose-vs-finding check."""
from __future__ import annotations

import pytest

from llm.verify import money_in, verify_claims

D1 = "11111111-2222-3333-4444-555555555555"
D2 = "66666666-7777-8888-9999-000000000000"
STRANGER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def finding(at_risk=None, quote=None):
    return {
        "closing_this_month": {"outcome": "not_requested"},
        "at_risk": at_risk or {"outcome": "not_requested"},
        "quote": quote or {"outcome": "not_requested"},
    }


def risk(**extra):
    return {"outcome": "answered", "deal_ids": [D1, D2], "total_value": 6817734.14,
            "open_without_close_date_ids": [], **extra}


# --- money_in ---------------------------------------------------------------

@pytest.mark.parametrize("text, amount", [
    ("₹2,64,000.00", 264000.0),
    ("Rs. 5799", 5799.0),
    ("264,000 INR", 264000.0),
    ("6,817,734.14", 6817734.14),
    ("5799.00", 5799.0),
])
def test_money_in_finds_amounts(text, amount):
    assert money_in(text) == [amount]


@pytest.mark.parametrize("text", ["9 deals", "12 days overdue", "500 units", "deal 42"])
def test_money_in_ignores_plain_counts(text):
    assert money_in(text) == []


# --- verify_claims: honest answers ------------------------------------------

def test_exact_indian_grouping_passes():
    check = verify_claims("Two deals are at risk, worth ₹68,17,734.14 in total.", finding(risk()))
    assert check["ok"] is True


def test_rounding_to_whole_rupees_passes():
    check = verify_claims("Two deals are at risk, worth ₹6,817,734.", finding(risk()))
    assert check["ok"] is True


def test_ids_from_the_finding_are_supported():
    text = f"At risk: {D1} and {D2}, worth ₹68,17,734.14."
    assert verify_claims(text, finding(risk()))["unsupported_ids"] == []


# --- verify_claims: invented facts ------------------------------------------

def test_unknown_uuid_is_unsupported():
    text = f"Deal {STRANGER} is at risk, total ₹68,17,734.14."
    check = verify_claims(text, finding(risk()))
    assert check["unsupported_ids"] == [STRANGER]
    assert check["ok"] is False


def test_unknown_escalation_number_is_unsupported():
    text = "Filed ESC-2026-00099. Total ₹68,17,734.14."
    check = verify_claims(text, finding(risk()))
    assert check["unsupported_escalations"] == ["ESC-2026-00099"]


def test_vague_million_amount_is_unsupported():
    check = verify_claims("About ₹6.8 million is at risk.", finding(risk()))
    assert check["unsupported_amounts"] == [6.8]


# --- verify_claims: omissions -----------------------------------------------

def test_missing_section_total():
    check = verify_claims("Two deals are at risk.", finding(risk()))
    assert check["missing"] == ["at_risk total 6,817,734.14"]


def test_missing_undated_count():
    f = finding(risk(open_without_close_date_ids=["u1", "u2", "u3"]))
    check = verify_claims("Two deals are at risk, worth ₹68,17,734.14.", f)
    assert "3 open deals have no expected close date" in check["missing"]


def test_partial_read_must_be_flagged():
    f = finding(risk(pagination_complete=False))
    assert verify_claims("₹68,17,734.14 at risk.", f)["missing"] == [
        "at_risk may be incomplete (pagination_complete is false)"]
    assert verify_claims("₹68,17,734.14 at risk (list may be partial).", f)["ok"] is True


def test_dry_run_escalation_must_say_so():
    f = finding(quote={"outcome": "escalated", "dry_run": True})
    check = verify_claims("The quote was escalated to a human.", f)
    assert check["missing"] == ["dry run: no escalation was actually filed"]


def test_dry_run_escalation_described_honestly():
    f = finding(quote={"outcome": "escalated", "dry_run": True})
    assert verify_claims("This was a dry run, so nothing was filed.", f)["ok"] is True
