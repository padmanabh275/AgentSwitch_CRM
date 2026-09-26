"""Item lookup, price check and the BOM-quote path. Zero LLM.

No BOM.* tool exists anywhere in this seat's tool list — a structural fact
confirmed by probing the live tool catalog, not re-checked per call. So a
quote either comes from a real sales-visible price on the Item record, or
it's escalated. It never invents a number.
"""
from __future__ import annotations

from domain.escalation import file_escalation, quote_subject
from transport.mcp_client import NOT_FOUND, MCPClient, MCPToolError

PRICE_FIELDS = ("default_rate", "selling_price", "standard_rate", "purchase_rate", "mrp")
ITEM_FIELDS = ("id", "name", "code", "uom", "is_sellable", "default_bom_id") + PRICE_FIELDS


def get_item(client: MCPClient, item_id: str) -> dict:
    """Item.get. Refuses only on a genuine not_found; raises otherwise."""
    try:
        item = client.call("Item.get", {"id": item_id})
    except MCPToolError as e:
        if e.code != NOT_FOUND:
            raise
        return {"outcome": "refused", "item_id": item_id,
                "reason": f"item {item_id} does not exist — nothing to quote"}
    return {"outcome": "answered", "item_id": item_id,
            "item": {k: item.get(k) for k in ITEM_FIELDS}}


def price_lookup(item_section: dict, qty: int) -> dict:
    """Pure: the quote section for `qty` of an already-fetched item.

    outcome is "quoted" when a price field is set, "unpriced" when none is
    (the caller decides whether to escalate), "refused" for a bad qty.

    A quoted price is the Item's *list* price — not a BOM-derived cost, and
    before tax, price breaks or discounts. price_source says which field.
    """
    item_id = item_section["item_id"]
    base = {"requested_qty": qty, "item_id": item_id}
    if not isinstance(qty, int) or qty <= 0:
        return {**base, "outcome": "refused",
                "reason": f"quantity must be a positive whole number, got {qty!r}"}

    item = item_section["item"]
    for field in PRICE_FIELDS:
        v = item.get(field)
        if v:
            price = float(v)
            return {**base, "outcome": "quoted", "unit_price": price,
                    "total_price": round(price * qty, 2), "price_source": f"Item.{field}",
                    "reason": (f"list price from Item.{field}; not a BOM-derived cost, "
                               "excludes tax and discounts")}

    return {**base, "outcome": "unpriced",
            "reason": ("no BOM.* tool available to this seat; sales-visible price "
                       f"fields ({', '.join(PRICE_FIELDS)}) are unset on item {item_id}")}


def escalate_quote(client: MCPClient, session_id: str | None, priced: dict,
                   dry_run: bool = False) -> dict:
    """File (or reuse) the escalation for an unpriced quote."""
    esc = file_escalation(client, session_id, reason=priced["reason"], reason_code="other",
                          subject=quote_subject(priced["item_id"], priced["requested_qty"]),
                          dry_run=dry_run)
    return {**esc, "requested_qty": priced["requested_qty"], "item_id": priced["item_id"],
            "reason": priced["reason"]}


def attempt_quote(client: MCPClient, session_id: str | None, item_id: str, qty: int,
                  dry_run: bool = False) -> dict:
    """get_item -> price_lookup -> escalate_quote in one call — the chat
    loop's tool shape. The graph runs the same three steps as nodes."""
    item = get_item(client, item_id)
    if item["outcome"] != "answered":
        return {**item, "requested_qty": qty}
    priced = price_lookup(item, qty)
    if priced["outcome"] != "unpriced":
        return priced
    return escalate_quote(client, session_id, priced, dry_run=dry_run)
