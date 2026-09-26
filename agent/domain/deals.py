"""Deal-list logic for the Sales/Pipeline seat (Team 6). Zero LLM, unit-testable.

Every function returns a dict shaped like one section of the finding schema
locked in SALES_AGENT_DESIGN.md. Deal.list has no server-side aggregate
(bug a81bd641: schema advertises limit<=1000 but the server silently caps
real pages at 50), so we paginate and filter ourselves.
"""
from __future__ import annotations

import datetime as dt

from transport.mcp_client import MCPClient, MCPToolError

PAGE_SIZE = 50
MAX_PAGES = 40  # 40 * 50 = 2000 deals — comfortably above the observed ~135
OPEN_FORECAST_STAGES = ("new", "qualification", "proposal", "negotiation")
CLOSED_STAGES = ("closed_won", "closed_lost")


def _paginate_deals(client: MCPClient) -> tuple[list[dict], bool]:
    """All Deal rows, paginated ourselves. Returns (deals, pagination_complete).

    pagination_complete is False only if we hit MAX_PAGES before exhausting
    the server's reported `total` — a safety valve, not an expected path.
    """
    deals: list[dict] = []
    offset = 0
    total = None
    for _ in range(MAX_PAGES):
        page = client.call("Deal.list", {"limit": PAGE_SIZE, "offset": offset})
        rows = page.get("data", [])
        deals.extend(rows)
        total = page.get("total", len(deals))
        offset += len(rows)
        if not rows or offset >= total:
            return deals, True
    return deals, False


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def list_closing_this_month(client: MCPClient, today: dt.date | None = None) -> dict:
    """Open-stage deals whose expected_close_date falls in the current
    calendar month. Deliberately excludes deals already closed_won this
    month — that's an "actuals" question, a different one (see design doc).
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        deals, complete = _paginate_deals(client)
    except MCPToolError as e:
        return {"outcome": "error", "month": today.strftime("%Y-%m"),
                "deal_ids": [], "total_value": 0.0, "pagination_complete": False,
                "reason": str(e)}

    matches = []
    for d in deals:
        if d.get("stage") not in OPEN_FORECAST_STAGES:
            continue
        close = _parse_date(d.get("expected_close_date"))
        if close and close.year == today.year and close.month == today.month:
            matches.append(d)

    return {
        "outcome": "answered",
        "month": today.strftime("%Y-%m"),
        "deal_ids": [d["id"] for d in matches],
        "total_value": round(sum(d.get("value") or 0.0 for d in matches), 2),
        "pagination_complete": complete,
    }


def list_at_risk(client: MCPClient, today: dt.date | None = None) -> dict:
    """Open deals whose expected_close_date is in the past.

    _rot_level/_rot_days were considered and ruled out as a risk signal —
    only ever none/fresh across a 100-row sample (see design doc).
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        deals, complete = _paginate_deals(client)
    except MCPToolError as e:
        return {"outcome": "error",
                "rule": "open deal with expected_close_date in the past",
                "deal_ids": [], "reasons": {}, "pagination_complete": False,
                "reason": str(e)}

    matches = []
    reasons = {}
    for d in deals:
        if d.get("stage") in CLOSED_STAGES:
            continue
        close = _parse_date(d.get("expected_close_date"))
        if close and close < today:
            matches.append(d)
            reasons[d["id"]] = (
                f"expected_close_date {close.isoformat()} passed, "
                f"still in stage '{d.get('stage')}'"
            )

    return {
        "outcome": "answered",
        "rule": "open deal with expected_close_date in the past",
        "deal_ids": [d["id"] for d in matches],
        "reasons": reasons,
        "pagination_complete": complete,
    }

