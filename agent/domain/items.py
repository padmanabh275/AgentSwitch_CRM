"""Item lookup, price check and the BOM-quote path. Zero LLM.

The BOM price is Item.standard_rate. No BOM.* tool exists for this seat,
but it doesn't need one: standard_rate is a read-only field on Item
(listed in `_readonly_fields` alongside default_bom_id and routing_id),
and on live data (2026-09-26) it is set on 27 items — every one of them
with a BOM, and never on an item without one. That is manufacturing's BOM
costing, readable from a Sales-owned entity; which field counts as "the
BOM price" is still to be confirmed with the instructor.

A quote is standard_rate x qty. When standard_rate is unset (42 of the 69
items with a BOM, and every item without one), the quote is escalated —
never filled in from a list price or anything else. It never invents a
number.
"""
from __future__ import annotations

import re

from domain.escalation import file_escalation, quote_subject
from transport.mcp_client import NOT_FOUND, MCPClient, MCPToolError

BOM_PRICE_FIELD = "standard_rate"
# Shown for context only (in the quote section and an escalation), never
# quoted: none of them is the BOM price the request asks for.
LIST_PRICE_FIELDS = ("default_rate", "selling_price", "mrp")
ITEM_FIELDS = ("id", "name", "code", "uom", "is_sellable", "is_manufactured",
               "default_bom_id", BOM_PRICE_FIELD, "standard_rate_updated_at") + LIST_PRICE_FIELDS


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


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MAX_CANDIDATES = 20


def resolve_item(client: MCPClient, ref: str) -> dict:
    """An item from whatever the user called it: id, code or (part of) a name.

    An id goes straight to Item.get. Otherwise Item.list's `search` (which
    matches name and code, verified live) is used, and:
      - one exact name/code match, or exactly one result -> that item
      - no results -> refused ("no item matches ...")
      - several -> refused, listing the candidates ("which one?")
    Picking one of several would risk quoting the wrong part.
    """
    ref = ref.strip()
    if _UUID.fullmatch(ref):
        return {**get_item(client, ref), "query": ref, "matched_by": "id"}

    page = client.call("Item.list", {"search": ref, "limit": MAX_CANDIDATES})
    rows = page.get("data") or []
    exact = [r for r in rows
             if ref.casefold() in ((r.get("name") or "").casefold(), (r.get("code") or "").casefold())]
    chosen = exact if len(exact) == 1 else rows if len(rows) == 1 else None
    if chosen:
        item = chosen[0]
        return {"outcome": "answered", "item_id": item["id"], "query": ref,
                "matched_by": "exact" if exact else "search",
                "item": {k: item.get(k) for k in ITEM_FIELDS}}

    base = {"outcome": "refused", "item_id": None, "query": ref}
    if not rows:
        return {**base, "reason": f"no item matches {ref!r} — nothing to quote"}
    total = page.get("total", len(rows))
    candidates = [{"id": r.get("id"), "name": r.get("name"), "code": r.get("code")} for r in rows]
    names = "; ".join(f"{c['name']} ({c['code']})" for c in candidates[:5])
    more = f" and {total - 5} more" if total > 5 else ""
    return {**base, "candidates": candidates,
            "reason": f"{ref!r} matches {total} items — which one? {names}{more}"}


def price_lookup(item_section: dict, qty: int) -> dict:
    """Pure: the quote section for `qty` of an already-fetched item.

    outcome is "quoted" when the item has a BOM price (standard_rate),
    "unpriced" when it doesn't (the caller decides whether to escalate),
    "refused" for a bad qty. The quote is the BOM price x qty — before tax,
    margin, price breaks or discounts.
    """
    item_id = item_section["item_id"]
    item = item_section["item"]
    base = {"requested_qty": qty, "item_id": item_id,
            "item_name": item.get("name"), "item_code": item.get("code")}
    if not isinstance(qty, int) or qty <= 0:
        return {**base, "outcome": "refused",
                "reason": f"quantity must be a positive whole number, got {qty!r}"}

    list_prices = {f: item.get(f) for f in LIST_PRICE_FIELDS if item.get(f)}
    bom_price = item.get(BOM_PRICE_FIELD)
    if bom_price:
        price = float(bom_price)
        updated = item.get("standard_rate_updated_at")
        return {**base, "outcome": "quoted", "unit_price": price,
                "total_price": round(price * qty, 2),
                "price_source": f"Item.{BOM_PRICE_FIELD}", "price_as_of": updated,
                "list_prices": list_prices,
                "reason": (f"BOM price from Item.{BOM_PRICE_FIELD} (manufacturing's BOM "
                           f"costing{', updated ' + updated if updated else ''}); "
                           "excludes tax and discounts")}

    why = ("has no BOM" if not item.get("default_bom_id")
           else "has a BOM but no BOM price (standard_rate) set yet")
    return {**base, "outcome": "unpriced", "list_prices": list_prices,
            "reason": (f"item {item.get('name') or item_id} {why}, so there's no real BOM "
                       "price to quote from; list prices aren't a substitute")}


def escalate_quote(client: MCPClient, session_id: str | None, priced: dict,
                   dry_run: bool = False) -> dict:
    """File (or reuse) the escalation for an unpriced quote."""
    esc = file_escalation(client, session_id, reason=priced["reason"], reason_code="other",
                          subject=quote_subject(priced["item_id"], priced["requested_qty"]),
                          dry_run=dry_run)
    return {**esc, **{k: priced.get(k) for k in ("requested_qty", "item_id", "item_name",
                                                 "item_code", "list_prices", "reason")}}


def attempt_quote(client: MCPClient, session_id: str | None, item_ref: str, qty: int,
                  dry_run: bool = False) -> dict:
    """resolve_item -> price_lookup -> escalate_quote in one call — the chat
    loop's tool shape. The graph runs the same three steps as nodes."""
    item = resolve_item(client, item_ref)
    if item["outcome"] != "answered":
        return {**item, "requested_qty": qty}
    priced = price_lookup(item, qty)
    if priced["outcome"] != "unpriced":
        return priced
    return escalate_quote(client, session_id, priced, dry_run=dry_run)
