"""Turn a quotation the customer accepted into a sales order. Zero LLM.

Probed live 2026-10-21 (T6-harness-probe quotes on our own deals):
- Quotation.accept.{sent,viewed}.accepted is a status change; accepting a
  draft, or accepting twice, is refused (invalid_transition).
- Quotation.convert_to_order.accepted.converted (accepted -> converted)
  creates the SalesOrder itself: one order, same party, lines and totals,
  quotation_id set. Quotation.make.SalesOrder also moves the quote to
  converted, and either way a second call makes no second order.
- Since Release 13 (N483, was drafts/BUG10) converting links the order to
  the quotation's deal (SalesOrder.deal_id) and the platform marks that
  deal closed_won itself. Before it, the order had no deal field and the
  deal stayed open. The seat can't delete or cancel an order.

So this agent makes the order only when the customer accepted:
- an `accepted` quotation is converted;
- a `sent`/`viewed` one is accepted and then converted, only if the user
  said the customer accepted it (customer_accepted); otherwise refused.
  The platform would convert a sent quote straight away
  (convert_to_order.sent.converted); this agent never uses that edge;
- anything else is refused: draft, pending approval, declined, expired,
  cancelled, already converted, an order already made from it, or a deal
  that is closed.

This agent never calls Deal.mark_won: the platform's conversion wins the
deal, and the deal is read back afterwards so the answer says what it is
now, never what it was assumed to be.

Because the conversion wins the deal, a deal closed by someone else between
the first read and the write must stop the order: converting would turn a
lost deal into a won one. So the deal and the quotation are both read again
just before the first write (after `before_write`, where the harness may
hold the run to make that change); a closed deal or a moved quotation is
refused with no write. Writes are never
retried (registry: retries=0). If the accept lands and the conversion
doesn't, that is reported as an error with the quotation left accepted.
"""
from __future__ import annotations

import re
from typing import Callable

from domain.deals import OPEN_STAGES
from transport.mcp_client import INVALID_TRANSITION, NOT_FOUND, MCPClient, MCPToolError

ACCEPT_TOOL = "Quotation.accept.{status}.accepted"
CONVERT_TOOL = "Quotation.convert_to_order.accepted.converted"
ACCEPTABLE = ("sent", "viewed")
QUOTE_FIELDS = ("id", "number", "status", "deal_id", "party_id", "net_total", "grand_total", "updated_at")
ORDER_FIELDS = ("id", "number", "status", "quotation_id", "deal_id", "party_id", "net_total",
                "grand_total")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NOT_ORDERABLE = {
    "draft": "it is a draft: it hasn't been sent to the customer, so they can't have accepted it",
    "pending_approval": "it is waiting for margin approval and hasn't been sent to the customer",
    "declined": "the customer declined it",
    "expired": "it has expired",
    "cancelled": "it was cancelled",
    "converted": "it has already been converted into an order",
}


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in keys}


def _unwrap(result: dict) -> dict:
    if isinstance(result, dict) and "id" not in result and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def find_quotation(client: MCPClient, ref: str) -> tuple[dict | None, str | None]:
    """(quotation, None) or (None, why not). By id, else by exact number
    (QTN-...). The number filter is checked here too."""
    ref = (ref or "").strip()
    if UUID_RE.fullmatch(ref):
        try:
            return _pick(_unwrap(client.call("Quotation.get", {"id": ref})), QUOTE_FIELDS), None
        except MCPToolError as e:
            if e.code != NOT_FOUND:
                raise
            return None, f"quotation {ref} does not exist"
    rows = client.call("Quotation.list", {"number": ref, "limit": 20}).get("data") or []
    same = [r for r in rows if (r.get("number") or "").strip().casefold() == ref.casefold()]
    if not same:
        return None, f"no quotation is numbered {ref!r}"
    if len(same) > 1:
        return None, f"{len(same)} quotations are numbered {ref!r}; give the quotation id"
    return _pick(_unwrap(client.call("Quotation.get", {"id": same[0]["id"]})), QUOTE_FIELDS), None


def orders_for(client: MCPClient, quotation_id: str) -> list[dict]:
    """Sales orders made from the quotation; the filter is checked here too."""
    rows = client.call("SalesOrder.list", {"quotation_id": quotation_id, "limit": 50}).get("data") or []
    return [_pick(r, ORDER_FIELDS) for r in rows if r.get("quotation_id") == quotation_id]


def plan(status: str | None, customer_accepted: bool) -> tuple[list[str] | None, str | None]:
    """(the transition tools to call, in order, None) or (None, why not)."""
    if status == "accepted":
        return [CONVERT_TOOL], None
    if status in ACCEPTABLE:
        if not customer_accepted:
            return None, (f"it was {status} but the customer hasn't accepted it; an order is made "
                          "only from an accepted quotation (tell me if the customer has accepted it)")
        return [ACCEPT_TOOL.format(status=status), CONVERT_TOOL], None
    return None, NOT_ORDERABLE.get(status, f"it is {status}")


def _changed_since_read(client: MCPClient, q: dict, label: str, base: dict) -> dict | None:
    """The pre-write re-read: a refusal if the deal closed or the quotation
    moved since they were first read, else None."""
    if q.get("deal_id"):
        deal = client.call("Deal.get", {"id": q["deal_id"]})
        if deal.get("stage") not in OPEN_STAGES:
            return {**base, "outcome": "refused", "deal_stage_now": deal.get("stage"),
                    "reason": f"no order was made from quotation {label}: its deal {deal.get('title')!r} "
                              f"was {base.get('deal_stage')} when read but is {deal.get('stage')} now; it "
                              "changed underneath this run, and converting would mark it won"}
    now = _pick(_unwrap(client.call("Quotation.get", {"id": q["id"]})), QUOTE_FIELDS)
    if now["status"] != q["status"]:
        return {**base, "outcome": "refused", "status_now": now["status"],
                "reason": f"no order was made from quotation {label}: it was {q['status']} when read "
                          f"but is {now['status']} now; it changed underneath this run"}
    return None


def accept_quote(client: MCPClient, quotation_ref: str, customer_accepted: bool = False,
                 dry_run: bool = False, before_write: Callable[[], dict | None] | None = None) -> dict:
    """outcome: ordered (order made and read back), would_order (dry run),
    refused (see the module doc), error (a step didn't read back).
    before_write runs between the first reads and the pre-write re-read."""
    base = {"quotation_ref": quotation_ref, "customer_accepted": bool(customer_accepted)}
    if not (quotation_ref or "").strip():
        return {**base, "outcome": "refused", "reason": "can't make an order without knowing which quotation"}
    q, why = find_quotation(client, quotation_ref)
    if q is None:
        return {**base, "outcome": "refused", "reason": why}
    label = q.get("number") or q["id"]
    base.update(quotation_id=q["id"], number=q.get("number"), status_before=q["status"],
                deal_id=q.get("deal_id"))
    steps, why = plan(q["status"], customer_accepted)
    if steps is None:
        return {**base, "outcome": "refused", "reason": f"no order was made from quotation {label}: {why}"}
    if q.get("deal_id"):
        deal = client.call("Deal.get", {"id": q["deal_id"]})
        base.update(deal_title=deal.get("title"), deal_stage=deal.get("stage"))
        if deal.get("stage") not in OPEN_STAGES:
            return {**base, "outcome": "refused",
                    "reason": f"no order was made from quotation {label}: its deal "
                              f"{deal.get('title')!r} is {deal.get('stage')}"}
    existing = orders_for(client, q["id"])
    if existing:
        return {**base, "outcome": "refused", "existing_orders": existing,
                "reason": f"no order was made from quotation {label}: order "
                          f"{existing[0]['number']} was already made from it"}
    names = {t.get("name") for t in client.list_tools()}
    missing = [t for t in steps if t not in names]
    if missing:
        return {**base, "outcome": "refused",
                "reason": f"no order was made from quotation {label}: no {missing[0]} in this seat's catalogue"}
    base["tools"] = steps
    if dry_run:
        return {**base, "outcome": "would_order", "dry_run": True,
                "reason": f"dry run: quotation {label} would be " + (
                    "accepted and " if len(steps) == 2 else "") + "converted into a sales order "
                    f"with {', then '.join(steps)}; nothing was written"}
    held = before_write() if before_write else None
    if held:
        base["harness_gate"] = held
    changed = _changed_since_read(client, q, label, base)
    if changed:
        return changed
    done: list[str] = []
    for tool in steps:
        try:
            client.call(tool, {"id": q["id"]})
        except MCPToolError as e:
            if e.code != INVALID_TRANSITION:
                raise
            now = _pick(_unwrap(client.call("Quotation.get", {"id": q["id"]})), QUOTE_FIELDS)
            if done:   # accepted, but the conversion didn't apply
                return {**base, "outcome": "error", "done": done, "status_now": now["status"],
                        "reason": f"quotation {label} was accepted, but {tool} was refused "
                                  f"(it is {now['status']} now): no order was made"}
            return {**base, "outcome": "refused", "status_now": now["status"],
                    "reason": f"quotation {label} was {q['status']} when read but is {now['status']} "
                              "now: it changed underneath this run, so no order was made"}
        done.append(tool)
    after = _pick(_unwrap(client.call("Quotation.get", {"id": q["id"]})), QUOTE_FIELDS)
    orders = orders_for(client, q["id"])
    base.update(done=done, status_after=after["status"])
    if after["status"] != "converted" or len(orders) != 1:
        return {**base, "outcome": "error", "orders": orders,
                "reason": f"{', then '.join(done)} returned, but quotation {label} reads back as "
                          f"{after['status']!r} with {len(orders)} order(s), not converted with 1"}
    so = orders[0]
    problems = [f"{k} {so.get(k)} != quotation {after.get(k)}"
                for k in ("party_id", "deal_id", "net_total", "grand_total") if so.get(k) != after.get(k)]
    out = {**base, "outcome": "ordered", "order_id": so["id"], "order_number": so.get("number"),
           "order_status": so.get("status"), "order_grand_total": so.get("grand_total"),
           "order_deal_id": so.get("deal_id"), "order_problems": problems,
           "reason": (f"{'accepted and ' if len(done) == 2 else ''}converted quotation {label} into "
                      f"sales order {so.get('number')} ({so.get('status')})"
                      + ("" if not problems else "; but the order differs: " + "; ".join(problems)))}
    if q.get("deal_id"):
        deal = client.call("Deal.get", {"id": q["deal_id"]})
        stage = deal.get("stage")
        out.update(deal_stage_after=stage)
        out["reason"] += (f"; the platform marked deal {deal.get('title')!r} {stage} when the order was made"
                          if stage == "closed_won" else
                          f"; deal {deal.get('title')!r} is still {stage} (this agent doesn't mark deals won)")
    return out
