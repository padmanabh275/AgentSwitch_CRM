"""parse_request(): a free-text question -> which parts of the seat's work it asks for.

The LLM only *classifies* the question. It never answers it, and everything
it returns is re-checked here: unknown asks are dropped, a missing or
non-positive quantity becomes None (never a guessed 500), and the route is
decided in code. The graph then refuses a quote with no item or no
quantity, rather than assuming either.
"""
from __future__ import annotations

import json

from transport.llm_client import LLMError, call_llm

ASKS = ("closing_this_month", "at_risk", "quote")

SCHEMA = {
    "type": "object",
    "properties": {
        "asks": {
            "type": "array",
            "items": {"type": "string", "enum": list(ASKS)},
            "description": "Which of the three standard questions the user asks.",
        },
        "item_ref": {
            "type": "string",
            "description": "The item to quote, exactly as the user wrote it (name, code or id). Empty if none.",
        },
        "qty": {
            "type": "integer",
            "description": "Units to quote, only if the user states a number. 0 if not stated.",
        },
        "out_of_scope": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "request": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["request", "reason"],
            },
            "description": "Parts that are not this sales agent's job at all.",
        },
        "other": {
            "type": "string",
            "description": "An in-scope sales/pipeline request not covered by asks (e.g. look up one deal or lead). Empty if none.",
        },
    },
    "required": ["asks", "item_ref", "qty", "out_of_scope", "other"],
}

SYSTEM_PROMPT = """You classify requests sent to the Pipeline/Sales agent of \
Suryodaya Precision Works. Do not answer the request; only classify it.

The agent has three standard questions (asks):
- closing_this_month: which deals are expected to close this month.
- at_risk: which deals are at risk (overdue, slipping).
- quote: a price for some quantity of an item.

Fill the fields:
- asks: every standard question the request contains.
- item_ref: for a quote, the item exactly as written (name, code or id). \
Never invent one; empty if the user didn't name an item.
- qty: for a quote, the number of units only if stated. Never assume a \
default; 0 if not stated.
- out_of_scope: parts that are not a sales agent's job at all, e.g. payroll, \
salaries or commissions, HR, accounting/invoices, changing or deleting \
records, or a discount beyond policy. Give a short reason for each.
- other: an in-scope sales request that isn't one of the three questions, \
e.g. "look up deal <id>" or "what's the status of lead X". Empty if none."""


class IntentError(Exception):
    pass


def validate(raw: dict) -> dict:
    """Normalise the LLM's classification. Pure; also used by tests."""
    if not isinstance(raw, dict):
        raise IntentError(f"intent is not an object: {raw!r}")
    asks = [a for a in ASKS if a in (raw.get("asks") or [])]   # known only, canonical order
    item_ref = (raw.get("item_ref") or "").strip() or None
    qty = raw.get("qty")
    qty = qty if isinstance(qty, int) and not isinstance(qty, bool) and qty > 0 else None
    out_of_scope = [
        {"request": str(o.get("request", "")).strip(), "reason": str(o.get("reason", "")).strip()}
        for o in (raw.get("out_of_scope") or []) if isinstance(o, dict) and o.get("request")
    ]
    other = (raw.get("other") or "").strip() or None

    if asks or (out_of_scope and not other):
        route = "graph"
    else:
        route = "chat"   # anything else in scope — or unclassifiable — goes to the tool loop
    return {"route": route, "asks": asks, "item_ref": item_ref, "qty": qty,
            "out_of_scope": out_of_scope, "other": other}


def parse_request(query: str) -> dict:
    """Raises IntentError if the gateway fails or returns something unusable."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query}]
    try:
        resp = call_llm(messages, max_tokens=512, response_schema=SCHEMA)
    except LLMError as e:
        raise IntentError(f"could not classify the request: {e}") from e
    raw = resp.get("parsed")
    if raw is None:
        try:
            raw = json.loads(resp.get("text") or "")
        except ValueError:
            raise IntentError(f"classifier returned no JSON: {(resp.get('text') or '')[:200]!r}")
    return validate(raw)
