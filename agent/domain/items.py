"""Item price lookup and the BOM-quote path. Zero LLM."""
from __future__ import annotations

from domain.escalation import file_escalation
from transport.mcp_client import MCPClient, MCPToolError


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

