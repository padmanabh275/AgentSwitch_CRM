"""The quote as a record: draft a Quotation on a deal, then send it. Zero LLM.

This is the request's state change ("...and quote 500 units"): the BOM
price from domain/items.py becomes a real Quotation on the deal, moved
draft -> sent by its transition tool. Each step re-reads before it writes,
because Team 07 shares the book:

1. read_open_deal: the deal exists and is open (new..negotiation).
2. create_quotation: re-read the deal right before the write. If it has
   closed since step 1, refuse: quoting a lost or won deal is wrong
   whatever the price. Otherwise Quotation.create, then read it back and
   compare the line and totals with what was sent.
3. send_quotation: re-read the quotation, send it only if it is still a
   draft and the read-back matched, then read it again to confirm `sent`.

Quotation.create is used, not Deal.make.Quotation: make copies the deal's
own products, and editing the deal's products to get 500 units on it
would change a record this agent doesn't own. Quotation.create with
deal_id links the quote without touching the deal.

No tax is added. The quote is the BOM price x qty, before tax and
discounts, as the quote section already says; GST needs place of supply
and the customer's registration, which the deal doesn't carry. Tax goes
in record-level taxes[] when it is added (per-line tax fields don't reach
the totals, bug a6899ff7).

Writes are never retried (registry: retries=0): a timed-out create may
have landed server-side.
"""
from __future__ import annotations

from domain.deals import OPEN_STAGES
from domain.escalation import T6_PREFIX
from domain.records import get_deal
from transport.mcp_client import MCPClient

DEAL_FIELDS = ("id", "title", "stage", "party_id", "company_id", "currency",
               "expected_close_date", "updated_at")
QUOTATION_FIELDS = ("id", "number", "deal_id", "status", "net_total", "total_tax",
                    "grand_total", "currency_code", "items")
MONEY_TOLERANCE = 0.01
# The send transition is `Quotation.send` on Suryodaya (walked 2026-09-22);
# Deal and Quotation transitions elsewhere are named verb.<from>.<to>, so
# both shapes are accepted, matched against the live catalogue.
SEND_TOOL = "Quotation.send"


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in keys}


def _unwrap(result: dict) -> dict:
    """Document-chain tools wrap the record as {status, result}; create and
    transitions return it directly (SALES_AGENT_PATH.md, trap 1)."""
    # {status, result} at minimum; Lead.make.Deal without products carries
    # more keys than that (live, 2026-10-15), so key on "no id at the top".
    if isinstance(result, dict) and "id" not in result \
            and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def read_open_deal(client: MCPClient, deal_id: str) -> dict:
    """The deal to quote on. Refuses if it doesn't exist or isn't open."""
    got = get_deal(client, deal_id)
    if got["outcome"] != "answered":
        return {**got, "deal_id": deal_id}
    deal = _pick(got["deal"], DEAL_FIELDS)
    if deal["stage"] not in OPEN_STAGES:
        return {"outcome": "refused", "deal_id": deal_id, "deal": deal,
                "reason": f"deal {deal['title']!r} is {deal['stage']}, not open — "
                          "a quotation on it would be wrong"}
    return {"outcome": "answered", "deal_id": deal_id, "deal": deal}


def quotation_args(deal: dict, priced: dict, run_id: str) -> dict:
    qty, rate = priced["requested_qty"], priced["unit_price"]
    return {
        "party_id": deal["party_id"],
        "company_id": deal["company_id"],
        "deal_id": deal["id"],
        "items": [{"item_id": priced["item_id"], "qty": qty, "rate": rate,
                   "amount": round(rate * qty, 2),
                   "description": f"{priced.get('item_name') or priced['item_id']} "
                                  f"(BOM price, {priced['price_source']})"}],
        "notes": (f"{T6_PREFIX}quote from the sales agent, run {run_id}: "
                  f"{priced['price_source']} x {qty}, before tax and discounts"),
    }


def _readback_problems(row: dict, args: dict) -> list[str]:
    """What the platform stored that differs from what was sent."""
    want = args["items"][0]
    lines = row.get("items") or []
    problems = []
    if len(lines) != 1:
        problems.append(f"{len(lines)} line(s) stored, 1 sent")
    else:
        got = lines[0]
        for k in ("item_id", "qty", "rate"):
            if got.get(k) != want[k] and not (
                    isinstance(want[k], float) and abs((got.get(k) or 0) - want[k]) <= MONEY_TOLERANCE):
                problems.append(f"line {k}: stored {got.get(k)!r}, sent {want[k]!r}")
    if abs((row.get("net_total") or 0) - want["amount"]) > MONEY_TOLERANCE:
        problems.append(f"net_total {row.get('net_total')} != {want['amount']}")
    expected_grand = round((row.get("net_total") or 0) + (row.get("total_tax") or 0), 2)
    if abs((row.get("grand_total") or 0) - expected_grand) > MONEY_TOLERANCE:
        problems.append(f"grand_total {row.get('grand_total')} != net + tax {expected_grand}")
    if row.get("deal_id") != args["deal_id"]:
        problems.append(f"deal_id stored {row.get('deal_id')!r}")
    return problems


def _reread_deal(client: MCPClient, first: dict, base: dict) -> tuple[dict | None, dict | None]:
    """The pre-write re-read: (fresh deal, None), or (None, refusal) if the
    deal closed since it was first read. Sets base["deal_changed_since_read"]."""
    fresh = read_open_deal(client, first["id"])
    if fresh["outcome"] != "answered":
        now = (fresh.get("deal") or {}).get("stage")
        return None, {**base, "outcome": "refused", "deal_stage_now": now,
                      "reason": (f"deal {first['title']!r} was {first['stage']} when read but is "
                                 f"{now} now: it changed underneath this run, so no quotation "
                                 "was drafted" if now else fresh["reason"])}
    deal = fresh["deal"]
    base["deal_changed_since_read"] = deal.get("updated_at") != first.get("updated_at")
    return deal, None


def create_quotation(client: MCPClient, deal_read: dict, priced: dict, run_id: str,
                     dry_run: bool = False) -> dict:
    """Re-read the deal, then draft the quotation and read it back.

    outcome: drafted (created and read back), would_create (dry run),
    refused (the deal closed since it was first read)."""
    first = deal_read["deal"]
    base = {"deal_id": first["id"], "deal_title": first["title"],
            "item_id": priced["item_id"], "requested_qty": priced["requested_qty"],
            "unit_price": priced["unit_price"], "expected_net_total": priced["total_price"],
            "deal_read_at": first.get("updated_at")}
    deal, refusal = _reread_deal(client, first, base)
    if refusal:
        return refusal
    args = quotation_args(deal, priced, run_id)
    if dry_run:
        return {**base, "outcome": "would_create", "dry_run": True, "would_create": args,
                "reason": "dry run: the quotation would be drafted; nothing was written"}

    created = _unwrap(client.call("Quotation.create", args))
    row = _unwrap(client.call("Quotation.get", {"id": created["id"]}))
    problems = _readback_problems(row, args)
    return {**base, "outcome": "drafted", "quotation": _pick(row, QUOTATION_FIELDS),
            "quotation_id": row.get("id"), "number": row.get("number"),
            "status": row.get("status"), "readback_problems": problems,
            "reason": ("drafted and read back" if not problems
                       else "drafted, but the stored quotation differs: " + "; ".join(problems))}


def send_tool_name(client: MCPClient) -> str | None:
    """The live name of the draft -> sent transition, or None."""
    names = {t.get("name") for t in client.list_tools()}
    if SEND_TOOL in names:
        return SEND_TOOL
    shaped = sorted(n for n in names if n and n.startswith(SEND_TOOL + ".draft."))
    return shaped[0] if shaped else None


def send_quotation(client: MCPClient, drafted: dict, dry_run: bool = False) -> dict:
    """draft -> sent, only for a quotation whose read-back matched.

    outcome: sent, would_send (dry run), refused (not safe to send)."""
    base = {k: drafted.get(k) for k in ("deal_id", "deal_title", "item_id", "requested_qty",
                                        "unit_price", "expected_net_total", "quotation_id",
                                        "number", "deal_changed_since_read")}
    if dry_run or drafted["outcome"] == "would_create":
        return {**drafted, "outcome": "would_send", "dry_run": True,
                "reason": "dry run: the quotation would be drafted and sent; nothing was written"}
    if drafted.get("readback_problems"):
        return {**base, "outcome": "refused", "status": drafted.get("status"),
                "reason": "not sent: " + drafted["reason"]}
    row = _unwrap(client.call("Quotation.get", {"id": drafted["quotation_id"]}))
    if row.get("status") != "draft":
        return {**base, "outcome": "refused", "status": row.get("status"),
                "reason": f"not sent: the quotation is {row.get('status')} now, not draft"}
    tool = send_tool_name(client)
    if tool is None:
        return {**base, "outcome": "refused", "status": "draft",
                "reason": "not sent: no Quotation.send transition in this seat's catalogue; "
                          "the quotation stays a draft"}
    client.call(tool, {"id": drafted["quotation_id"]})
    after = _unwrap(client.call("Quotation.get", {"id": drafted["quotation_id"]}))
    status = after.get("status")
    if status != "sent":
        return {**base, "outcome": "error", "status": status, "send_tool": tool,
                "reason": f"{tool} returned, but the quotation reads back as {status!r}"}
    return {**base, "outcome": "sent", "status": status, "send_tool": tool,
            "quotation": _pick(after, QUOTATION_FIELDS),
            "reason": f"drafted and sent with {tool}; read back as sent"}


# ---------------------------------------------------------------- margin approval
#
# The route for an item this seat can't price. The seat can't read
# standard_rate (redacted on Item.list/get by 2026-10-05; visible on
# 2026-09-26) and, as noticed 2026-10-15, has no AgentEscalation.create;
# Quotation gained draft -> pending_approval (submit_for_margin_approval) ->
# sent (release_approved_quote), with the cost basis and margin fields
# redacted from us. So the quote goes on the
# deal with qty and item but no rate, and is submitted for margin approval:
# whoever can see the cost prices it. This seat never sends a rate on this
# route, so it can't invent one; whatever rate the platform stores on the
# line is reported as the platform's, not ours.

SUBMIT_TOOL = "Quotation.submit_for_margin_approval.draft.pending_approval"


def approval_args(deal: dict, priced: dict, run_id: str) -> dict:
    qty = priced["requested_qty"]
    return {
        "party_id": deal["party_id"],
        "company_id": deal["company_id"],
        "deal_id": deal["id"],
        "items": [{"item_id": priced["item_id"], "qty": qty,
                   "description": f"{priced.get('item_name') or priced['item_id']} "
                                  "(no rate: priced at margin approval)"}],
        "notes": (f"{T6_PREFIX}quote from the sales agent, run {run_id}: {qty} units, "
                  "unpriced (this seat can't see the BOM price); submitted for margin approval"),
    }


def _approval_problems(row: dict, args: dict) -> list[str]:
    want = args["items"][0]
    lines = row.get("items") or []
    problems = []
    if len(lines) != 1:
        problems.append(f"{len(lines)} line(s) stored, 1 sent")
    else:
        for k in ("item_id", "qty"):
            if lines[0].get(k) != want[k]:
                problems.append(f"line {k}: stored {lines[0].get(k)!r}, sent {want[k]!r}")
    if row.get("deal_id") != args["deal_id"]:
        problems.append(f"deal_id stored {row.get('deal_id')!r}")
    return problems


def line_rate_required(client: MCPClient) -> bool:
    """Whether the live Quotation.create schema requires items[].rate.

    Read from tools/list on every run, not assumed: on 2026-10-15 it was
    required, which makes the unpriced route impossible for this seat."""
    for tool in client.list_tools():
        if tool.get("name") == "Quotation.create":
            line = (((tool.get("inputSchema") or {}).get("properties") or {})
                    .get("items") or {}).get("items") or {}
            return "rate" in (line.get("required") or [])
    return False


def draft_for_approval(client: MCPClient, deal_read: dict, priced: dict, run_id: str,
                       dry_run: bool = False) -> dict:
    """Re-read the deal, then draft an unpriced quotation on it and read it back.

    outcome: drafted, would_create (dry run), refused (the deal closed, or the
    platform requires a line rate this seat can't see)."""
    first = deal_read["deal"]
    base = {"route": "margin_approval", "deal_id": first["id"], "deal_title": first["title"],
            "item_id": priced["item_id"], "requested_qty": priced["requested_qty"],
            "unpriced_reason": priced.get("reason"), "deal_read_at": first.get("updated_at")}
    deal, refusal = _reread_deal(client, first, base)
    if refusal:
        return refusal
    if line_rate_required(client):
        # No placeholder rate (e.g. 0) by default: a priced-looking line on a
        # customer document is an invented price. See docs/TO_REVISIT.md #11.
        return {**base, "outcome": "refused", "rate_required": True,
                "reason": "not drafted: Quotation.create requires a rate on every line, and "
                          "this seat can't see the item's BOM price (standard_rate is "
                          "redacted), so any rate would be invented; nothing was written"}
    args = approval_args(deal, priced, run_id)
    if dry_run:
        return {**base, "outcome": "would_create", "dry_run": True, "would_create": args,
                "reason": "dry run: an unpriced quotation would be drafted for margin "
                          "approval; nothing was written"}

    created = _unwrap(client.call("Quotation.create", args))
    row = _unwrap(client.call("Quotation.get", {"id": created["id"]}))
    problems = _approval_problems(row, args)
    line = (row.get("items") or [{}])[0]
    return {**base, "outcome": "drafted", "quotation": _pick(row, QUOTATION_FIELDS),
            "quotation_id": row.get("id"), "number": row.get("number"),
            "status": row.get("status"), "readback_problems": problems,
            "platform_rate": line.get("rate"),
            "reason": ("drafted without a rate and read back" if not problems
                       else "drafted, but the stored quotation differs: " + "; ".join(problems))}


def submit_tool_name(client: MCPClient) -> str | None:
    names = {t.get("name") for t in client.list_tools()}
    return SUBMIT_TOOL if SUBMIT_TOOL in names else None


def submit_for_approval(client: MCPClient, drafted: dict, dry_run: bool = False) -> dict:
    """draft -> pending_approval, only for a draft whose read-back matched.

    outcome: pending_approval, would_submit (dry run), refused, error."""
    base = {k: drafted.get(k) for k in ("route", "deal_id", "deal_title", "item_id",
                                        "requested_qty", "unpriced_reason", "quotation_id",
                                        "number", "platform_rate", "deal_changed_since_read")}
    if dry_run or drafted["outcome"] == "would_create":
        return {**drafted, "outcome": "would_submit", "dry_run": True,
                "reason": "dry run: an unpriced quotation would be drafted and submitted for "
                          "margin approval; nothing was written"}
    if drafted.get("readback_problems"):
        return {**base, "outcome": "refused", "status": drafted.get("status"),
                "reason": "not submitted: " + drafted["reason"]}
    row = _unwrap(client.call("Quotation.get", {"id": drafted["quotation_id"]}))
    if row.get("status") != "draft":
        return {**base, "outcome": "refused", "status": row.get("status"),
                "reason": f"not submitted: the quotation is {row.get('status')} now, not draft"}
    tool = submit_tool_name(client)
    if tool is None:
        return {**base, "outcome": "refused", "status": "draft",
                "reason": "not submitted: no margin-approval transition in this seat's "
                          "catalogue; the quotation stays a draft"}
    client.call(tool, {"id": drafted["quotation_id"]})
    after = _unwrap(client.call("Quotation.get", {"id": drafted["quotation_id"]}))
    status = after.get("status")
    if status != "pending_approval":
        return {**base, "outcome": "error", "status": status, "submit_tool": tool,
                "reason": f"{tool} returned, but the quotation reads back as {status!r}"}
    return {**base, "outcome": "pending_approval", "status": status, "submit_tool": tool,
            "quotation": _pick(after, QUOTATION_FIELDS),
            "reason": "drafted without a rate and submitted for margin approval; "
                      "read back as pending_approval"}
