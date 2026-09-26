"""The allowlist: every operation a graph node can perform.

A TaskSpec names one of these entries, never a raw MCP tool, and
GraphStore.apply rejects anything else — so neither the fixed plan nor a
planner can reach Deal.mark_lost.*, Quotation.convert_to_order.* or any
other write this registry doesn't expose. The one write here,
escalate_quote, is dry-run aware and never retried.

Node functions take (ctx, args, outcomes) where `outcomes` maps node id ->
NodeOutcome for everything finished so far. Which upstream node a function
reads is named in its args (e.g. {"snapshot": "snapshot_deals"}), so the
wiring is visible in the checkpoint rather than implied by node ids.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from domain import deals, items
from graph.finding import build_finding
from graph.outcome import ANSWERED, NodeOutcome
from llm import narrate
from transport.mcp_client import MCPClient


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


REGISTRY: dict[str, NodeDef] = {
    "snapshot_deals": NodeDef(
        fn=lambda ctx, a, o: deals.snapshot_deals(ctx.client),
        description="Read every Deal once (paginated, deduped).",
        retries=2),
    "closing_this_month": NodeDef(
        fn=lambda ctx, a, o: deals.closing_this_month(_data(o, a["snapshot"]), ctx.today),
        description="Open deals closing in the current IST month."),
    "at_risk": NodeDef(
        fn=lambda ctx, a, o: deals.at_risk(_data(o, a["snapshot"]), ctx.today),
        description="Open deals whose close date has passed."),
    "get_item": NodeDef(
        fn=lambda ctx, a, o: items.get_item(ctx.client, a["item_id"]),
        description="Item.get; refuses on not_found.",
        retries=2),
    "price_lookup": NodeDef(
        fn=lambda ctx, a, o: items.price_lookup(_data(o, a["item"]), a["qty"]),
        description="Quote from the Item's list price, or report it unpriced."),
    "escalate_quote": NodeDef(
        fn=lambda ctx, a, o: items.escalate_quote(ctx.client, ctx.session_id,
                                                  _data(o, a["priced"]), dry_run=ctx.dry_run),
        description="File (or reuse) an escalation for an unpriced quote.",
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
