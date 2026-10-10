"""The curated tool menu handed to the LLM — not raw MCP passthrough.

Deliberately excludes every write/transition/bypass tool (Deal.mark_lost.*,
Quotation.convert_to_order.*, etc.) from the LLM's reach by construction: the
LLM can only ever call the seven functions below, and each one is a thin,
audited wrapper over domain/ (refuse_request touches nothing: it only
records the refusal in the finding).
"""
from __future__ import annotations

import datetime as dt
from typing import Callable

from domain import deals, escalation, items, records
from transport.mcp_client import MCPClient

TOOL_SPECS: list[dict] = [
    {
        "name": "list_closing_this_month",
        "description": (
            "List open-stage deals (new/qualification/proposal/negotiation) "
            "whose expected_close_date falls in the current calendar month. "
            "Paginates and filters against live data — never a guess."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_at_risk",
        "description": (
            "List open deals whose expected_close_date is already in the "
            "past. Paginates and filters against live data."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "attempt_quote",
        "description": (
            "Quote a quantity of an item off its real BOM price (the item's "
            "standard_rate, set by manufacturing's BOM costing). If the item "
            "has no BOM price, this escalates rather than guessing — it never "
            "invents a number or substitutes a list price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string",
                         "description": "the item to quote: its name, code or id, as the user gave it"},
                "qty": {"type": "integer", "description": "requested quantity"},
            },
            "required": ["item", "qty"],
        },
    },
    {
        "name": "file_escalation",
        "description": (
            "Escalate a request to a human (Meera Kulkarni) when this seat "
            "can't do something itself but someone else's access plausibly "
            "can. Do not call this for requests that are not this agent's "
            "job at all — refuse those instead, without filing anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "reason_code": {
                    "type": "string",
                    "enum": ["unresolved_after_retries", "customer_asked_for_a_person",
                             "policy_refusal", "sensitive_topic", "agent_error", "other"],
                },
                "subject": {"type": "string"},
                "party_id": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "refuse_request",
        "description": (
            "Record that you are refusing (part of) the request: it is not "
            "this agent's job, it needs a capability no tool here has, or "
            "the id it names doesn't exist. Call it once per refused part, "
            "then say so in your answer. Files nothing and changes nothing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {"type": "string",
                            "description": "the part of the request being refused, briefly"},
                "reason": {"type": "string", "description": "why it is refused"},
            },
            "required": ["request", "reason"],
        },
    },
    {
        "name": "get_deal",
        "description": (
            "Look up one deal by id. If the id doesn't exist, this reports "
            "that plainly — never invent a deal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_lead",
        "description": (
            "Look up one lead by id. If the id doesn't exist, this reports "
            "that plainly — never invent a lead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]


def build_dispatch(client: MCPClient, session_id: str | None, dry_run: bool = False,
                   today: dt.date | None = None) -> dict[str, Callable[[dict], dict]]:
    """name -> callable(arguments dict) -> result dict, bound to this run's
    client/session. dry_run suppresses the one write the LLM can trigger
    (AgentEscalation.create); reads still happen."""
    return {
        "list_closing_this_month": lambda args: deals.list_closing_this_month(client, today),
        "list_at_risk": lambda args: deals.list_at_risk(client, today),
        "attempt_quote": lambda args: items.attempt_quote(
            client, session_id, args["item"], int(args["qty"]), dry_run=dry_run),
        "file_escalation": lambda args: escalation.file_escalation(
            client, session_id, reason=args["reason"],
            reason_code=args.get("reason_code", "other"),
            subject=args.get("subject"), party_id=args.get("party_id"), dry_run=dry_run),
        "refuse_request": lambda args: refusal(args.get("request", ""), args.get("reason", "")),
        "get_deal": lambda args: records.get_deal(client, args["id"]),
        "get_lead": lambda args: records.get_lead(client, args["id"]),
    }


def refusal(request: str, reason: str) -> dict:
    """A refusal in the graph's shape (registry "refuse" node), so the
    finding's `refusals` list reads the same on either path."""
    return {"outcome": "refused", "request": str(request).strip(), "reason": str(reason).strip()}
