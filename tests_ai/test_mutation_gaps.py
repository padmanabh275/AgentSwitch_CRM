"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Two behaviours that no test caught when the code was broken on purpose
(docs/TEST_EVIDENCE.md):

1. "Today" is the IST date. At 00:30 IST on 1 October it is still
   30 September in UTC; using the UTC date would make "closing this month"
   list September's deals.
2. The harness's deal-set check fails when the agent's per-currency total
   disagrees with the platform's values for the deals it named.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from domain import deals
from harness.verifiers import RunFiles, check_deal_set

UTC = dt.timezone.utc


class _Clock(dt.datetime):
    """datetime whose now() is 30 Sep 2026 19:00 UTC = 1 Oct 2026 00:30 IST."""

    @classmethod
    def now(cls, tz=None):
        moment = dt.datetime(2026, 9, 30, 19, 0, tzinfo=UTC)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


def _freeze(monkeypatch):
    clock = SimpleNamespace(datetime=_Clock, date=dt.date, timedelta=dt.timedelta, timezone=dt.timezone)
    monkeypatch.setattr(deals, "dt", clock)


def test_today_is_the_ist_date_not_utc(monkeypatch):
    _freeze(monkeypatch)
    assert deals.today_ist() == dt.date(2026, 10, 1)


def test_closing_this_month_defaults_to_the_ist_month(monkeypatch):
    _freeze(monkeypatch)
    snapshot = {"pagination_complete": True, "fetched_at": "2026-10-01T00:30:00+05:30", "deals": [
        {"id": "sep", "stage": "proposal", "expected_close_date": "2026-09-30", "value": 10.0, "currency": "INR"},
        {"id": "oct", "stage": "proposal", "expected_close_date": "2026-10-20", "value": 20.0, "currency": "INR"},
    ]}
    result = deals.closing_this_month(snapshot)
    assert (result["month"], result["deal_ids"]) == ("2026-10", ["oct"])


# --- deal-set total --------------------------------------------------------------

TODAY = dt.date(2026, 9, 28)
ROWS = [
    {"id": "d1", "stage": "proposal", "expected_close_date": "2026-09-10", "value": 1000.0,
     "currency": "INR", "updated_at": "2026-09-01T00:00:00+05:30"},
    {"id": "d2", "stage": "negotiation", "expected_close_date": "2026-09-20", "value": 500.0,
     "currency": "INR", "updated_at": "2026-09-01T00:00:00+05:30"},
]


def _files(claimed_total: float) -> RunFiles:
    obs = {"observations": {"deals": {"total": len(ROWS), "rows": ROWS}}}
    finding = {"closing_this_month": {"outcome": "answered", "deal_ids": ["d1", "d2"],
                                      "total_by_currency": {"INR": claimed_total},
                                      "snapshot_at": "2026-09-28T10:00:00+05:30"}}
    return RunFiles(run_dir=Path("."), task={}, chat_trace=None, graph=None, calls=[],
                    taskrun={"today": TODAY.isoformat(), "dry_run": True, "finding": finding},
                    before=obs, after=obs)


def _total_status(claimed_total: float) -> str:
    checks = check_deal_set(_files(claimed_total), {"section": "closing_this_month"})
    return next(c.status for c in checks if c.name == "total")


def test_deal_set_total_passes_when_it_matches_the_platform():
    assert _total_status(1500.0) == "pass"


def test_deal_set_total_fails_when_the_agent_reports_a_wrong_total():
    assert _total_status(2500.0) == "fail"
