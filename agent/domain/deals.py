"""Deal-list logic for the Sales/Pipeline seat (Team 6). Zero LLM, unit-testable.

One paginated read (`snapshot_deals`) feeds both questions, so "closing
this month" and "at risk" always describe the same state of a book that
Team 07 also writes to. The two filters are pure functions over that
snapshot — no client, no clock unless you pass `today`.

Deal.list has no server-side aggregate (bug a81bd641: schema advertises
limit<=1000 but the server silently caps real pages at 50), so we
paginate and filter ourselves, and say so when the read may be partial.
"""
from __future__ import annotations

import datetime as dt

from transport.mcp_client import MCPClient

PAGE_SIZE = 50
MAX_PAGES = 40  # 40 * 50 = 2000 deals — comfortably above the observed ~138
OPEN_STAGES = ("new", "qualification", "proposal", "negotiation")
CLOSED_STAGES = ("closed_won", "closed_lost")

# Suryodaya is an Indian company; "this month" and "overdue" are judged on
# the IST calendar, not UTC (they differ 00:00–05:30 IST). India has no DST,
# so a fixed offset is exact.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

AT_RISK_RULE = "open deal with expected_close_date in the past"


def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def is_open(deal: dict) -> bool:
    """The one definition of "open" both lists use. A stage outside the
    known six is neither open nor closed — it's reported by the snapshot
    as unknown_stage_ids rather than silently counted either way."""
    return deal.get("stage") in OPEN_STAGES


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def snapshot_deals(client: MCPClient) -> dict:
    """Every Deal row, read once. Raises MCPToolError if a page fails.

    pagination_complete is True only if the server reported a `total` and
    we hold at least that many distinct ids. A page without `total`, the
    MAX_PAGES valve, or rows shifting under offset paging while someone
    else writes (duplicates dropped, so fewer distinct ids) all leave it
    False — a partial read is never presented as a complete one.
    """
    by_id: dict[str, dict] = {}
    offset = 0
    total = None
    duplicates = 0
    for _ in range(MAX_PAGES):
        page = client.call("Deal.list", {"limit": PAGE_SIZE, "offset": offset})
        rows = page.get("data") or []
        total = page.get("total")
        for row in rows:
            if row.get("id") in by_id:
                duplicates += 1
            by_id[row.get("id")] = row
        offset += len(rows)
        if not rows or (total is not None and offset >= total):
            break
        if total is None and len(rows) < PAGE_SIZE:
            break  # short page with no total: probably the end, but unproven

    deals = list(by_id.values())
    known = OPEN_STAGES + CLOSED_STAGES
    return {
        "outcome": "answered",
        "fetched_at": dt.datetime.now(IST).isoformat(),
        "deals": deals,
        "total_reported": total,
        "pagination_complete": total is not None and len(deals) >= total,
        "duplicates_dropped": duplicates,
        "unknown_stage_ids": [d.get("id") for d in deals if d.get("stage") not in known],
    }


def _summary(deal: dict, today: dt.date | None = None) -> dict:
    """What a person needs to recognise a deal — not just its UUID."""
    out = {k: deal.get(k) for k in ("id", "title", "party_id", "stage", "value",
                                    "currency", "expected_close_date",
                                    "probability", "owner", "updated_at")}
    close = _parse_date(deal.get("expected_close_date"))
    if today and close and close < today:
        out["days_overdue"] = (today - close).days
    return out


def _totals(deals: list[dict]) -> tuple[float | None, dict[str, float]]:
    """Sum of `value`, per currency. total_value is None when currencies
    are mixed — adding INR to USD would be an invented number."""
    by_ccy: dict[str, float] = {}
    for d in deals:
        ccy = d.get("currency") or "unknown"
        by_ccy[ccy] = round(by_ccy.get(ccy, 0.0) + (d.get("value") or 0.0), 2)
    if len(by_ccy) > 1:
        return None, by_ccy
    return (next(iter(by_ccy.values())) if by_ccy else 0.0), by_ccy


def closing_this_month(snapshot: dict, today: dt.date | None = None) -> dict:
    """Open deals whose expected_close_date falls in the current IST
    calendar month. Deliberately excludes deals already closed_won this
    month — that's an "actuals" question, a different one (see design doc).
    """
    today = today or today_ist()
    matches = []
    for d in snapshot["deals"]:
        close = _parse_date(d.get("expected_close_date"))
        if is_open(d) and close and (close.year, close.month) == (today.year, today.month):
            matches.append(d)
    matches.sort(key=lambda d: d.get("value") or 0.0, reverse=True)

    total_value, by_ccy = _totals(matches)
    return {
        "outcome": "answered",
        "month": today.strftime("%Y-%m"),
        "deal_ids": [d["id"] for d in matches],
        "deals": [_summary(d) for d in matches],
        "total_value": total_value,
        "total_by_currency": by_ccy,
        "pagination_complete": snapshot["pagination_complete"],
        "snapshot_at": snapshot["fetched_at"],
    }


def at_risk(snapshot: dict, today: dt.date | None = None) -> dict:
    """Open deals whose expected_close_date is in the past, largest first.

    _rot_level/_rot_days were considered and ruled out as a risk signal —
    only ever none/fresh across a 100-row sample (see design doc).
    Open deals with no close date at all aren't flagged as at risk (that
    would change the rule), but are listed separately so they aren't
    silently invisible.
    """
    today = today or today_ist()
    matches = []
    no_close_date = []
    reasons = {}
    for d in snapshot["deals"]:
        if not is_open(d):
            continue
        close = _parse_date(d.get("expected_close_date"))
        if close is None:
            no_close_date.append(d["id"])
        elif close < today:
            matches.append(d)
            reasons[d["id"]] = (
                f"expected_close_date {close.isoformat()} passed "
                f"({(today - close).days} days ago), still in stage '{d.get('stage')}'"
            )
    matches.sort(key=lambda d: d.get("value") or 0.0, reverse=True)

    total_value, by_ccy = _totals(matches)
    return {
        "outcome": "answered",
        "rule": AT_RISK_RULE,
        "as_of": today.isoformat(),
        "deal_ids": [d["id"] for d in matches],
        "deals": [_summary(d, today) for d in matches],
        "reasons": reasons,
        "total_value": total_value,
        "total_by_currency": by_ccy,
        "open_without_close_date_ids": no_close_date,
        "pagination_complete": snapshot["pagination_complete"],
        "snapshot_at": snapshot["fetched_at"],
    }


def list_closing_this_month(client: MCPClient, today: dt.date | None = None) -> dict:
    """Snapshot + filter in one call — the chat loop's tool shape."""
    return closing_this_month(snapshot_deals(client), today)


def list_at_risk(client: MCPClient, today: dt.date | None = None) -> dict:
    """Snapshot + filter in one call — the chat loop's tool shape."""
    return at_risk(snapshot_deals(client), today)
