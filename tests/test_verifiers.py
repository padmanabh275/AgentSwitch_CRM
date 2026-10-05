"""harness/verifiers.py: the scorer's own rules, checked against domain/ and saved runs."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from domain import deals
from harness.verifiers import RunFiles, _expected_quote, _rule_ids, check_deal_set

TODAY = dt.date(2026, 9, 28)
SNAPSHOT_AT = "2026-09-28T10:00:00+05:30"


def row(id, stage="proposal", close="2026-09-20", value=100.0, currency="INR",
        updated_at="2026-09-01T00:00:00+05:30"):
    return {"id": id, "title": f"Deal {id}", "stage": stage, "expected_close_date": close,
            "value": value, "currency": currency, "updated_at": updated_at}


def run_files(finding, before_rows, after_rows):
    def obs(rows):
        return {"observations": {"deals": {"total": len(rows), "rows": rows}}}
    return RunFiles(run_dir=Path("."), task={}, chat_trace=None, graph=None, calls=[],
                    taskrun={"today": TODAY.isoformat(), "dry_run": True, "finding": finding},
                    before=obs(before_rows), after=obs(after_rows))


# --- _rule_ids agrees with domain/deals ---------------------------------------

ROWS = [
    row("overdue_this_month", close="2026-09-05"),
    row("due_today", close="2026-09-28"),
    row("next_month", close="2026-10-01"),
    row("last_month", close="2026-08-31"),
    row("last_year", close="2025-09-15"),
    row("won", stage="closed_won", close="2026-09-10"),
    row("lost", stage="closed_lost", close="2026-01-01"),
    row("on_hold", stage="on_hold", close="2026-09-10"),
    row("no_date", close=None),
    row("bad_date", close="soon"),
    row("timestamp", stage="negotiation", close="2026-09-15T10:30:00"),
]


@pytest.mark.parametrize("section, domain_fn", [
    ("closing_this_month", deals.closing_this_month),
    ("at_risk", deals.at_risk),
])
def test_rule_ids_matches_domain(section, domain_fn):
    snapshot = {"deals": ROWS, "pagination_complete": True, "fetched_at": SNAPSHOT_AT}
    assert _rule_ids(section, ROWS, TODAY) == set(domain_fn(snapshot, TODAY)["deal_ids"])


def test_rule_ids_expected_sets():
    assert _rule_ids("closing_this_month", ROWS, TODAY) == {
        "overdue_this_month", "due_today", "timestamp"}
    assert _rule_ids("at_risk", ROWS, TODAY) == {
        "overdue_this_month", "last_month", "last_year", "timestamp"}


# --- _expected_quote ----------------------------------------------------------

def item(id="i1", name="Bench Vice", code="BV-1", standard_rate=None, default_bom_id=None):
    return {"id": id, "name": name, "code": code, "standard_rate": standard_rate,
            "default_bom_id": default_bom_id}


def test_expected_quote_not_found_is_refused():
    outcome, found, _ = _expected_quote({"error_code": "not_found"}, "some-id", 5)
    assert (outcome, found) == ("refused", None)


def test_expected_quote_other_error_is_an_error():
    assert _expected_quote({"error_code": "transient"}, "vice", 5)[0] == "error"


def test_expected_quote_ambiguous_search_is_refused():
    obs = {"kind": "search", "total": 3, "rows": [
        item("i1", "Bench Vice 4in"), item("i2", "Bench Vice 6in"), item("i3", "Bench Vice 8in")]}
    outcome, _, why = _expected_quote(obs, "bench vice", 5)
    assert outcome == "refused"
    assert "3 items" in why


def test_expected_quote_exact_match_among_several_is_chosen():
    obs = {"kind": "search", "total": 2, "rows": [item("i1", "Bench Vice", standard_rate=10),
                                                  item("i2", "Bench Vice 4in")]}
    outcome, found, _ = _expected_quote(obs, "bench vice", 5)
    assert outcome == "quoted" and found["id"] == "i1"


def test_expected_quote_empty_search_is_refused():
    assert _expected_quote({"kind": "search", "total": 0, "rows": []}, "lathe", 5)[0] == "refused"


def test_expected_quote_priced_item_is_quoted():
    outcome, found, _ = _expected_quote({"kind": "get", "row": item(standard_rate=12.5)}, "i1", 5)
    assert outcome == "quoted" and found["id"] == "i1"


@pytest.mark.parametrize("bom, why", [(None, "no BOM"), ("b1", "BOM but no standard_rate")])
def test_expected_quote_unpriced_item_is_escalated(bom, why):
    obs = {"kind": "get", "row": item(default_bom_id=bom)}
    assert _expected_quote(obs, "i1", 5) == ("escalated", obs["row"], why)


@pytest.mark.parametrize("ref, qty", [(None, 5), ("", 5), ("vice", 0), ("vice", -1),
                                      ("vice", 2.5), ("vice", None)])
def test_expected_quote_without_ref_or_valid_qty_is_refused(ref, qty):
    assert _expected_quote(None, ref, qty)[0] == "refused"


# --- check_deal_set: drift versus fail ----------------------------------------

def ids_check(updated_at):
    finding = {"closing_this_month": {
        "outcome": "answered", "deal_ids": ["x"], "snapshot_at": SNAPSHOT_AT,
        "total_by_currency": {"INR": 100.0}}}
    rows = [row("x", stage="closed_won", close="2026-09-10", updated_at=updated_at)]
    checks = check_deal_set(run_files(finding, rows, rows), {"section": "closing_this_month"})
    return next(c for c in checks if c.name == "ids")


def test_deal_changed_after_snapshot_is_drift():
    assert ids_check("2026-09-28T11:00:00+05:30").status == "drift"


def test_deal_unchanged_since_snapshot_is_fail():
    assert ids_check("2026-09-01T00:00:00+05:30").status == "fail"


def test_matching_deal_set_passes():
    finding = {"closing_this_month": {
        "outcome": "answered", "deal_ids": ["x"], "snapshot_at": SNAPSHOT_AT,
        "total_by_currency": {"INR": 100.0}}}
    rows = [row("x", close="2026-09-29")]
    checks = check_deal_set(run_files(finding, rows, rows), {"section": "closing_this_month"})
    assert [(c.name, c.status) for c in checks] == [("ids", "pass"), ("total", "pass")]


def test_unanswered_section_is_skipped():
    finding = {"closing_this_month": {"outcome": "error"}}
    checks = check_deal_set(run_files(finding, [], []), {"section": "closing_this_month"})
    assert [c.status for c in checks] == ["skip"]
