"""parse_request(): a free-text question -> which parts of the seat's work it asks for.

The LLM only *classifies* the question. It never answers it, and everything
it returns is re-checked here: unknown asks are dropped, a missing or
non-positive quantity becomes None (never a guessed 500), and the route is
decided in code. The graph then refuses a quote with no item or no
quantity, rather than assuming either.
"""
from __future__ import annotations

import json

from transport.llm_client import LLMError, call_llm, parse_json_object, with_schema_instruction

ASKS = ("closing_this_month", "at_risk", "quote")

# The JSON is under 200 tokens, but a reasoning model can spend most of a
# small budget before writing it: the hosted run's model (deepseek-v4p1-flash)
# returned empty content at 512. The retry asks again with more room and the
# schema in the prompt instead of JSON mode.
MAX_TOKENS = 2048
RETRY_MAX_TOKENS = 4096

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
            "description": "Parts the agent must refuse: not a sales agent's job, or an action it has no tool for.",
        },
        "other": {
            "type": "string",
            "description": "An in-scope request the agent can do that isn't one of the asks: looking up one deal or lead. Empty if none.",
        },
    },
    "required": ["asks", "item_ref", "qty", "out_of_scope", "other"],
}

SYSTEM_PROMPT = """You classify requests sent to the Pipeline/Sales agent of \
Suryodaya Precision Works. Do not answer the request; only classify it.

The agent can do only these things: answer its three standard questions, \
look up one deal or lead, and escalate to a human colleague what it can't \
do itself (e.g. a quote it can't price). \
It cannot send email or messages, start follow-up sequences, raise or send \
invoices, or create, change, merge or delete records.

The agent's three standard questions (asks):
- closing_this_month: which deals are expected to close this month.
- at_risk: which deals are at risk (overdue, slipping).
- quote: a price for some quantity of an item.

Fill the fields:
- asks: every standard question the request contains.
- item_ref: for a quote, the item exactly as written (name, code or id). \
Never invent one; empty if the user didn't name an item.
- qty: for a quote, the number of units only if stated. Never assume a \
default; 0 if not stated.
- out_of_scope: every part the agent must refuse, with a short reason each: \
(a) not a sales agent's job at all, e.g. payroll, salaries or commissions, \
HR, accounting, or a discount beyond policy; (b) an action the agent has no \
tool for, even if it is sales work, e.g. emailing a quotation, starting a \
follow-up sequence, raising an invoice, marking a deal lost or merging \
records (reason: no tool for it).
- other: an in-scope request the agent can do that isn't one of the three \
questions, e.g. "look up deal <id>" or "what's the status of lead X". Never \
put an action from (b) here. Empty if none."""


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


def classifier_fallback(error: str) -> dict:
    """The intent to run with when classification failed: the chat loop, with
    nothing pre-decided. `classifier_error` keeps the reason on the record."""
    return {"route": "chat", "asks": [], "item_ref": None, "qty": None,
            "out_of_scope": [], "other": None, "classifier_error": error}


def _reply_json(resp: dict):
    """The classification in a reply, or None. Valid JSON that isn't an object
    is returned as is, so validate() can say so."""
    raw = resp.get("parsed")
    if raw is not None:
        return raw
    text = resp.get("text") or ""
    try:
        return json.loads(text)
    except ValueError:
        return parse_json_object(text)   # tolerates prose or a code fence around it


def parse_request(query: str) -> dict:
    """Raises IntentError if the gateway fails or returns something unusable.

    A reply with no JSON (empty, cut off, prose) is asked once more with a
    bigger budget and the schema written into the prompt; the error names each
    attempt's stop_reason and length, so a repeat failure says why.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query}]
    attempts = [(messages, MAX_TOKENS, SCHEMA),
                (with_schema_instruction(messages, SCHEMA), RETRY_MAX_TOKENS, None)]
    failures = []
    for msgs, max_tokens, schema in attempts:
        try:
            resp = call_llm(msgs, max_tokens=max_tokens, response_schema=schema)
        except LLMError as e:
            raise IntentError(f"could not classify the request: {e}") from e
        raw = _reply_json(resp)
        if raw is not None:
            return validate(raw)
        text = resp.get("text") or ""
        failures.append(f"attempt {len(failures) + 1} (max_tokens={max_tokens}): "
                        f"stop_reason={resp.get('stop_reason')!r}, {len(text)} chars {text[:120]!r}")
    raise IntentError("classifier returned no JSON: " + "; ".join(failures))
