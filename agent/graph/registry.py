"""The allowlist: every operation a graph node can perform.

A TaskSpec names one of these entries, never a raw MCP tool, and
GraphStore.apply rejects anything else — so neither the fixed plan nor a
planner can reach Deal.mark_lost.*, Quotation.convert_to_order.* or any
other write this registry doesn't expose. The one write here,
escalate_quote, is dry-run aware and never retried. So are the two
quotation writes, create_quotation and send_quotation: they run only when
the request names a deal (run.py --deal) and the item has a BOM price.
For an unpriced item on a named deal, draft_for_approval and
submit_for_approval take the margin-approval route instead (no rate sent).
advance_deal moves an open deal exactly one stage forward (new ->
qualification -> proposal -> negotiation); won and lost stay out of reach.
advance_lead does the same for a lead (new -> contacted -> qualified ->
converted, which creates its deal). accept_quote turns a quotation the
customer accepted into a sales order (accept if the user says so, then
convert_to_order.accepted); never the sent -> converted shortcut.

Node functions take (ctx, args, outcomes) where `outcomes` maps node id ->
NodeOutcome for everything finished so far. Which upstream node a function
reads is named in its args (e.g. {"snapshot": "snapshot_deals"}), so the
wiring is visible in the checkpoint rather than implied by node ids.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from domain import deal_stage, deals, items, lead_stage, quotations, quote_order
from graph.finding import build_finding
from graph.outcome import ANSWERED, NodeOutcome
from harness import gate
from llm import narrate
from transport.mcp_client import MCPClient, MCPToolError


@dataclass
class RunContext:
    client: MCPClient
    run_id: str
    session_id: str | None
    query: str
    dry_run: bool = False
    today: dt.date | None = None   # None = today on the IST calendar


@dataclass(frozen=True)
class NodeDef:
    fn: Callable[[RunContext, dict, dict[str, NodeOutcome]], Any]
    description: str
    writes: bool = False
    retries: int = 0   # retries on a transient MCP error


def _data(outcomes: dict[str, NodeOutcome], node_id: str) -> dict:
    return outcomes[node_id].data


def _first_answered(outcomes: dict[str, NodeOutcome], node_ids: str | list[str]) -> dict:
    """Data of the first listed node that answered — e.g. a planner-added
    re-read of the deals ahead of the original read."""
    for node_id in [node_ids] if isinstance(node_ids, str) else node_ids:
        out = outcomes.get(node_id)
        if out is not None and out.status == ANSWERED:
            return out.data
    raise ValueError(f"none of {node_ids} answered")


def _create_quotation(ctx: RunContext, a: dict, o: dict[str, NodeOutcome]) -> dict:
    """The harness may hold the run here (harness/gate.py), between the
    first deal read and the pre-write re-read, to change the deal."""
    held = gate.wait_at("before_quotation_write")
    out = quotations.create_quotation(ctx.client, _data(o, a["deal"]), _data(o, a["priced"]),
                                      ctx.run_id, dry_run=ctx.dry_run)
    return {**out, "harness_gate": held} if held else out


def _draft_for_approval(ctx: RunContext, a: dict, o: dict[str, NodeOutcome]) -> dict:
    """The same harness gate as _create_quotation: the unpriced route's write."""
    held = gate.wait_at("before_quotation_write")
    out = quotations.draft_for_approval(ctx.client, _data(o, a["deal"]), _data(o, a["priced"]),
                                        ctx.run_id, dry_run=ctx.dry_run)
    return {**out, "harness_gate": held} if held else out


def _reread_deals(ctx: RunContext, previous: dict) -> dict:
    """A second full read after an incomplete one. It never fails the
    branch: if the re-read errors, the first (partial, flagged) read stands."""
    try:
        fresh = deals.snapshot_deals(ctx.client)
    except MCPToolError as e:
        return {**previous, "reread_error": f"{e.code}: {e}"}
    return {**fresh, "reread": True}


REGISTRY: dict[str, NodeDef] = {
    "snapshot_deals": NodeDef(
        fn=lambda ctx, a, o: deals.snapshot_deals(ctx.client),
        description="Read every Deal once (paginated, deduped).",
        retries=2),
    "reread_deals": NodeDef(
        fn=lambda ctx, a, o: _reread_deals(ctx, _data(o, a["previous"])),
        description="Re-read every Deal once after an incomplete snapshot."),
    "closing_this_month": NodeDef(
        fn=lambda ctx, a, o: deals.closing_this_month(_first_answered(o, a["snapshot"]), ctx.today),
        description="Open deals closing in the current IST month."),
    "at_risk": NodeDef(
        fn=lambda ctx, a, o: deals.at_risk(_first_answered(o, a["snapshot"]), ctx.today),
        description="Open deals whose close date has passed."),
    "get_item": NodeDef(
        fn=lambda ctx, a, o: items.get_item(ctx.client, a["item_id"]),
        description="Item.get; refuses on not_found.",
        retries=2),
    "resolve_item": NodeDef(
        fn=lambda ctx, a, o: items.resolve_item(ctx.client, a["ref"]),
        description="Find the item by id, code or name; refuses on no/ambiguous match.",
        retries=2),
    "price_lookup": NodeDef(
        fn=lambda ctx, a, o: items.price_lookup(_data(o, a["item"]), a["qty"]),
        description="Quote from the Item's list price, or report it unpriced."),
    "escalate_quote": NodeDef(
        fn=lambda ctx, a, o: items.escalate_quote(ctx.client, ctx.session_id,
                                                  _data(o, a["priced"]), dry_run=ctx.dry_run),
        description="File (or reuse) an escalation for an unpriced quote.",
        writes=True),
    "get_deal": NodeDef(
        fn=lambda ctx, a, o: quotations.read_open_deal(ctx.client, a["deal_id"]),
        description="Deal.get; refuses if it doesn't exist or isn't open.",
        retries=2),
    "create_quotation": NodeDef(
        fn=_create_quotation,
        description="Re-read the deal, then Quotation.create at the BOM price and read it back.",
        writes=True),
    "send_quotation": NodeDef(
        fn=lambda ctx, a, o: quotations.send_quotation(ctx.client, _data(o, a["drafted"]),
                                                       dry_run=ctx.dry_run),
        description="Quotation draft -> sent, only if still a draft and the read-back matched.",
        writes=True),
    "draft_for_approval": NodeDef(
        fn=_draft_for_approval,
        description="Re-read the deal, then Quotation.create with no rate (item unpriced) and read it back.",
        writes=True),
    "submit_for_approval": NodeDef(
        fn=lambda ctx, a, o: quotations.submit_for_approval(ctx.client, _data(o, a["drafted"]),
                                                            dry_run=ctx.dry_run),
        description="Quotation draft -> pending_approval (margin approval prices it).",
        writes=True),
    "advance_deal": NodeDef(
        fn=lambda ctx, a, o: deal_stage.advance_deal(ctx.client, a["deal_ref"], a.get("target_stage"),
                                                     dry_run=ctx.dry_run),
        description="Move an open deal one stage forward, never to won/lost; read it back.",
        writes=True),
    "advance_lead": NodeDef(
        fn=lambda ctx, a, o: lead_stage.advance_lead(ctx.client, a["lead_ref"], a.get("target_status"),
                                                     dry_run=ctx.dry_run),
        description="Move an open lead one status forward (convert makes its deal); read it back.",
        writes=True),
    "accept_quote": NodeDef(
        fn=lambda ctx, a, o: quote_order.accept_quote(
            ctx.client, a["quotation_ref"], bool(a.get("customer_accepted")), dry_run=ctx.dry_run,
            before_write=lambda: gate.wait_at("before_order_write")),
        description="Accepted quotation -> sales order (accept first only if the customer did).",
        writes=True),
    "refuse": NodeDef(
        fn=lambda ctx, a, o: {"outcome": "refused", **a},
        description="A refusal decided at planning time (args carry the reason)."),
    "build_finding": NodeDef(
        fn=lambda ctx, a, o: NodeOutcome(ANSWERED, data=build_finding(a, o, ctx)),
        description="Assemble the gradable finding from whatever finished."),
    "narrate": NodeDef(
        fn=lambda ctx, a, o: narrate.narrate(ctx.query, _data(o, a["finding"])),
        description="Turn the finding into prose (LLM, template fallback)."),
}
