"""Real logic for the Sales/Pipeline seat (Team 6). Zero LLM, unit-testable.

Every function returns a dict shaped like one section of the finding schema
locked in SALES_AGENT_DESIGN.md ("Structured finding schema" — FINAL). The
LLM never sees raw MCP responses for the list questions — only these
already-paginated, already-filtered results — because Deal.list has no
server-side filter or aggregate (bug a81bd641: schema advertises
limit<=1000 but the server silently caps real pages at 50, so a naive
single call under-reports without any error).
"""
from __future__ import annotations

import datetime as dt

from mcp_client import MCPClient, MCPToolError

PAGE_SIZE = 50
MAX_PAGES = 40  # 40 * 50 = 2000 deals — comfortably above the observed ~135
OPEN_FORECAST_STAGES = ("new", "qualification", "proposal", "negotiation")
CLOSED_STAGES = ("closed_won", "closed_lost")

ASSIGNEE_EMAIL = "meera.kulkarni@suryodaya.in"  # only entry in
# GET /api/agent-governance/escalations/assignees — see design doc.


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


def _sales_price(item: dict) -> float:
    """The first nonzero sales-visible price field, or 0.0.

    On real sampled data every one of these is 0.0 — no fallback price
    exists even if we wanted to fudge one (we won't).
    """
    for field in ("default_rate", "selling_price", "standard_rate", "purchase_rate", "mrp"):
        v = item.get(field)
        if v:
            return float(v)
    return 0.0


def attempt_quote(client: MCPClient, session_id: str, item_id: str, qty: int) -> dict:
    """Tries the BOM-dependent quote path; escalates or refuses when it can't.

    No BOM.* tool exists anywhere in this seat's tool list — that's a
    structural fact confirmed by probing the live tool catalog, not
    something re-checked per call. So this either finds a real
    sales-visible price on the Item record, or it escalates. It never
    invents a number.
    """
    try:
        item = client.call("Item.get", {"id": item_id})
    except MCPToolError:
        return {"outcome": "refused", "requested_qty": qty, "item_id": item_id,
                "reason": f"item {item_id} does not exist — nothing to quote"}

    price = _sales_price(item)
    if price:
        return {"outcome": "quoted", "requested_qty": qty, "item_id": item_id,
                "unit_price": price, "total_price": round(price * qty, 2),
                "reason": "real sales-visible price found on Item record"}

    reason = ("no BOM.* tool available to this seat; sales-visible price "
              f"fields are unset on item {item_id}")
    esc = file_escalation(client, session_id, reason=reason,
                           reason_code="other", subject=f"BOM quote for {qty}x {item_id}")
    return {"outcome": "escalated", "requested_qty": qty, "item_id": item_id,
            "reason": reason, "escalation_id": esc.get("escalation_id")}


def file_escalation(client: MCPClient, session_id: str, reason: str,
                     reason_code: str = "other", subject: str | None = None,
                     party_id: str | None = None) -> dict:
    """Wraps AgentEscalation.create. reason_code defaults to "other" because
    the enum has no value for a cross-app capability gap (bug candidate E1) —
    the single most predictable escalation reason on a seat-scoped platform.
    """
    args = {"session_id": session_id, "reason": reason, "reason_code": reason_code}
    if subject:
        args["subject"] = subject
    if party_id:
        args["party_id"] = party_id
    result = client.call("AgentEscalation.create", args)
    return {"escalation_id": result.get("id"), "assignee": ASSIGNEE_EMAIL}


def get_deal(client: MCPClient, deal_id: str) -> dict:
    """Generic read (flexibility valve). Also the pure-refuse showcase: a
    nonexistent deal ID gets a plain refusal, never a guessed answer.
    """
    try:
        deal = client.call("Deal.get", {"id": deal_id})
    except MCPToolError:
        return {"outcome": "refused", "id": deal_id,
                "reason": f"deal {deal_id} does not exist"}
    return {"outcome": "answered", "id": deal_id, "deal": deal}


def get_lead(client: MCPClient, lead_id: str) -> dict:
    """Generic read (flexibility valve). Same nonexistent-ID refusal as get_deal."""
    try:
        lead = client.call("Lead.get", {"id": lead_id})
    except MCPToolError:
        return {"outcome": "refused", "id": lead_id,
                "reason": f"lead {lead_id} does not exist"}
    return {"outcome": "answered", "id": lead_id, "lead": lead}
