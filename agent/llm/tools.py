"""The curated tool menu handed to the LLM — not raw MCP passthrough.

Deliberately excludes every write/transition/bypass tool (Deal.mark_lost.*,
Quotation.convert_to_order.*, etc.) from the LLM's reach by construction: the
LLM can only ever call the six functions below, and each one is a thin,
audited wrapper over domain/.
"""
from __future__ import annotations

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
            "Try to quote a real price for a quantity of an item, from the "
            "item's actual sales-visible price fields. If no real price "
            "exists, this escalates to manufacturing rather than guessing — "
            "it never invents a number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "Item.id to quote"},
                "qty": {"type": "integer", "description": "requested quantity"},
            },
            "required": ["item_id", "qty"],
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


def build_dispatch(client: MCPClient, session_id: str) -> dict[str, Callable[[dict], dict]]:
    """name -> callable(arguments dict) -> result dict, bound to this run's client/session."""
    return {
        "list_closing_this_month": lambda args: deals.list_closing_this_month(client),
        "list_at_risk": lambda args: deals.list_at_risk(client),
        "attempt_quote": lambda args: items.attempt_quote(
            client, session_id, args["item_id"], int(args["qty"])),
        "file_escalation": lambda args: escalation.file_escalation(
            client, session_id, reason=args["reason"],
            reason_code=args.get("reason_code", "other"),
            subject=args.get("subject"), party_id=args.get("party_id")),
        "get_deal": lambda args: records.get_deal(client, args["id"]),
        "get_lead": lambda args: records.get_lead(client, args["id"]),
    }
