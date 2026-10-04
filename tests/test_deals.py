"""domain/deals.py: the snapshot read and the two filters over it."""
from __future__ import annotations

import datetime as dt

import pytest

from domain import deals
from transport.mcp_client import TRANSIENT, MCPToolError

TODAY = dt.date(2026, 10, 15)


def deal(id, stage="proposal", close="2026-10-20", value=100.0, currency="INR", **extra):
    return {"id": id, "stage": stage, "expected_close_date": close, "value": value,
            "currency": currency, "title": f"Deal {id}", **extra}


def snapshot(rows, complete=True):
    return {"deals": rows, "pagination_complete": complete,
            "fetched_at": "2026-10-15T10:00:00+05:30"}


def paged(rows, total="len", page_size=deals.PAGE_SIZE):
    """A Deal.list handler that serves `rows` by limit/offset. total="len"
    reports len(rows); None omits it, as a page without a total would."""
    def handler(args):
        page = rows[args["offset"]:args["offset"] + min(args["limit"], page_size)]
        out = {"data": page}
        if total is not None:
            out["total"] = len(rows) if total == "len" else total
        return out
    return handler


# --- is_open / _parse_date -------------------------------------------------

@pytest.mark.parametrize("stage", deals.OPEN_STAGES)
def test_open_stages_are_open(stage):
    assert deals.is_open({"stage": stage})


@pytest.mark.parametrize("stage", deals.CLOSED_STAGES + ("on_hold", None))
def test_closed_and_unknown_stages_are_not_open(stage):
    assert not deals.is_open({"stage": stage})


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-04", dt.date(2026, 10, 4)),
    ("2026-10-04T23:00:00Z", dt.date(2026, 10, 4)),  # timestamps keep their date part
    (None, None),
    ("", None),
    ("next week", None),
])
def test_parse_date(raw, expected):
    assert deals._parse_date(raw) == expected


# --- snapshot_deals ---------------------------------------------------------

def test_snapshot_pages_until_total_and_is_complete(fake_client):
    rows = [deal(f"d{i}") for i in range(120)]
    client = fake_client({"Deal.list": paged(rows)})

    snap = deals.snapshot_deals(client)

    assert [a["offset"] for _, a in client.calls] == [0, 50, 100]
    assert len(snap["deals"]) == 120
    assert snap["total_reported"] == 120
    assert snap["pagination_complete"] is True
    assert snap["duplicates_dropped"] == 0
    assert snap["outcome"] == "answered"


def test_snapshot_of_empty_table_is_complete(fake_client):
    client = fake_client({"Deal.list": paged([])})
    snap = deals.snapshot_deals(client)
    assert snap["deals"] == [] and snap["pagination_complete"] is True


def test_snapshot_without_total_is_never_complete(fake_client):
    rows = [deal(f"d{i}") for i in range(70)]
    client = fake_client({"Deal.list": paged(rows, total=None)})

    snap = deals.snapshot_deals(client)

    assert len(snap["deals"]) == 70  # stopped on the short second page
    assert len(client.calls) == 2
    assert snap["pagination_complete"] is False


def test_snapshot_counts_duplicates_and_reports_incomplete(fake_client):
    # Someone inserts a row mid-read: offset paging serves d49 twice and
    # the last row is never seen.
    pages = [
        {"data": [deal(f"d{i}") for i in range(50)], "total": 100},
        {"data": [deal(f"d{i}") for i in range(49, 99)], "total": 100},
    ]
    client = fake_client({"Deal.list": pages})

    snap = deals.snapshot_deals(client)

    assert snap["duplicates_dropped"] == 1
    assert len(snap["deals"]) == 99
    assert snap["pagination_complete"] is False


def test_snapshot_stops_at_max_pages(fake_client, monkeypatch):
    monkeypatch.setattr(deals, "MAX_PAGES", 2)
    rows = [deal(f"d{i}") for i in range(500)]
    client = fake_client({"Deal.list": paged(rows)})

    snap = deals.snapshot_deals(client)

    assert len(client.calls) == 2
    assert len(snap["deals"]) == 100
    assert snap["pagination_complete"] is False


def test_snapshot_lists_unknown_stages(fake_client):
    rows = [deal("a"), deal("b", stage="on_hold"), deal("c", stage="closed_won")]
    snap = deals.snapshot_deals(fake_client({"Deal.list": paged(rows)}))
    assert snap["unknown_stage_ids"] == ["b"]


def test_snapshot_propagates_a_failed_page(fake_client):
    client = fake_client({"Deal.list": [MCPToolError("boom", code=TRANSIENT)]})
    with pytest.raises(MCPToolError) as e:
        deals.snapshot_deals(client)
    assert e.value.code == TRANSIENT


# --- _totals ----------------------------------------------------------------

def test_totals_single_currency_and_rounding():
    total, by_ccy = deals._totals([deal("a", value=0.1), deal("b", value=0.2), deal("c", value=None)])
    assert total == 0.3
    assert by_ccy == {"INR": 0.3}


def test_totals_mixed_currencies_has_no_single_total():
    total, by_ccy = deals._totals([deal("a", value=10), deal("b", value=5, currency="USD")])
    assert total is None
    assert by_ccy == {"INR": 10.0, "USD": 5.0}


def test_totals_of_nothing():
    assert deals._totals([]) == (0.0, {})


# --- closing_this_month -----------------------------------------------------

def test_closing_this_month_filters_open_deals_in_the_month():
    rows = [
        deal("in", close="2026-10-31", value=10),
        deal("first_day", close="2026-10-01", value=30),
        deal("won", stage="closed_won", close="2026-10-20"),
        deal("lost", stage="closed_lost", close="2026-10-20"),
        deal("next_month", close="2026-11-01"),
        deal("last_month", close="2026-09-30"),
        deal("last_year", close="2025-10-15"),
        deal("no_date", close=None),
        deal("unknown_stage", stage="on_hold", close="2026-10-20"),
    ]
    out = deals.closing_this_month(snapshot(rows), TODAY)

    assert out["deal_ids"] == ["first_day", "in"]  # largest value first
    assert out["month"] == "2026-10"
    assert out["total_value"] == 40.0
    assert out["deals"][0]["title"] == "Deal first_day"
    assert "days_overdue" not in out["deals"][0]


def test_closing_this_month_includes_dates_already_past_in_the_month():
    # A deal due on the 3rd and still open is both closing this month and at risk.
    rows = [deal("early", close="2026-10-03")]
    assert deals.closing_this_month(snapshot(rows), TODAY)["deal_ids"] == ["early"]
    assert deals.at_risk(snapshot(rows), TODAY)["deal_ids"] == ["early"]


def test_closing_this_month_passes_through_pagination_state():
    out = deals.closing_this_month(snapshot([], complete=False), TODAY)
    assert out["pagination_complete"] is False
    assert out["snapshot_at"] == "2026-10-15T10:00:00+05:30"
    assert out["total_value"] == 0.0


def test_closing_this_month_mixed_currency():
    rows = [deal("a", value=10), deal("b", value=20, currency="USD")]
    out = deals.closing_this_month(snapshot(rows), TODAY)
    assert out["total_value"] is None
    assert out["total_by_currency"] == {"INR": 10.0, "USD": 20.0}


# --- at_risk ----------------------------------------------------------------

def test_at_risk_is_open_and_past_close_date():
    rows = [
        deal("late", close="2026-10-05", value=10),
        deal("very_late", close="2026-01-15", value=50, stage="negotiation"),
        deal("due_today", close="2026-10-15"),
        deal("future", close="2026-12-01"),
        deal("closed_late", stage="closed_lost", close="2026-01-01"),
    ]
    out = deals.at_risk(snapshot(rows), TODAY)

    assert out["deal_ids"] == ["very_late", "late"]
    assert out["deals"][0]["days_overdue"] == 273
    assert out["deals"][1]["days_overdue"] == 10
    assert out["reasons"]["late"] == (
        "expected_close_date 2026-10-05 passed (10 days ago), still in stage 'proposal'")
    assert out["rule"] == deals.AT_RISK_RULE
    assert out["as_of"] == "2026-10-15"
    assert out["total_value"] == 60.0


def test_at_risk_lists_open_deals_without_a_close_date_separately():
    rows = [deal("none", close=None), deal("blank", close=""),
            deal("closed_none", stage="closed_won", close=None)]
    out = deals.at_risk(snapshot(rows), TODAY)
    assert out["deal_ids"] == []
    assert out["open_without_close_date_ids"] == ["none", "blank"]


def test_at_risk_treats_an_unparseable_date_as_missing():
    out = deals.at_risk(snapshot([deal("bad", close="soon")]), TODAY)
    assert out["deal_ids"] == []
    assert out["open_without_close_date_ids"] == ["bad"]


# --- list_* wrappers --------------------------------------------------------

def test_list_wrappers_read_once_and_filter(fake_client):
    rows = [deal("late", close="2026-10-01"), deal("later", close="2026-10-30")]
    client = fake_client({"Deal.list": paged(rows)})

    assert deals.list_closing_this_month(client, TODAY)["deal_ids"] == ["late", "later"]
    assert deals.list_at_risk(client, TODAY)["deal_ids"] == ["late"]
    assert client.tools_called() == ["Deal.list", "Deal.list"]
