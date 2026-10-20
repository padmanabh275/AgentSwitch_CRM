"""parse_request(): a free-text question -> which parts of the seat's work it asks for.

The LLM only *classifies* the question. It never answers it, and everything
it returns is re-checked here: unknown asks are dropped, a missing or
non-positive quantity becomes None (never a guessed 500), and the route is
decided in code. The graph then refuses a quote with no item or no
quantity, rather than assuming either.
"""
from __future__ import annotations

import json
import re

from transport.llm_client import LLMError, call_llm, parse_json_object, with_schema_instruction

ASKS = ("closing_this_month", "at_risk", "quote")
OPEN_STAGES = ("new", "qualification", "proposal", "negotiation")
LEAD_STATUSES = ("new", "contacted", "qualified", "converted")
# What a model writes for a lead step, live 2026-10-21: "convert it into a
# deal" came back as target_status "deal". Mapped, never guessed further.
LEAD_STATUS_SYNONYMS = {
    "deal": "converted", "a_deal": "converted", "convert": "converted", "conversion": "converted",
    "converted_to_deal": "converted", "converted_into_deal": "converted",
    "contact": "contacted", "qualify": "qualified",
}

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
        "advance_deal": {
            "type": "object",
            "properties": {
                "deal_ref": {"type": "string"},
                "target_stage": {"type": "string"},
            },
            "required": ["deal_ref", "target_stage"],
            "description": "A request to move one open deal forward a stage: the deal exactly as written (id or title) and the stage asked for, if any. Both empty if not asked.",
        },
        "advance_lead": {
            "type": "object",
            "properties": {
                "lead_ref": {"type": "string"},
                "target_status": {"type": "string"},
            },
            "required": ["lead_ref", "target_status"],
            "description": "A request to move one open lead forward a step (contacted, qualified, or converted into a deal): the lead exactly as written and the status asked for, if any. Both empty if not asked.",
        },
        "other": {
            "type": "string",
            "description": "An in-scope request the agent can do that isn't one of the asks: looking up one deal or lead. Empty if none.",
        },
    },
    "required": ["asks", "item_ref", "qty", "out_of_scope", "advance_deal", "advance_lead", "other"],
}

SYSTEM_PROMPT = """You classify requests sent to the Pipeline/Sales agent of \
Suryodaya Precision Works. Do not answer the request; only classify it.

The agent can do only these things: answer its three standard questions, \
look up one deal or lead, move one open deal forward a stage in the funnel \
(new -> qualification -> proposal -> negotiation), move one open lead \
forward a step (new -> contacted -> qualified -> converted into a deal), and \
escalate to a human colleague what it can't do itself (e.g. a quote it \
can't price). \
It cannot mark a deal won or lost, disqualify a lead, send email or \
messages, start follow-up sequences, raise or send invoices, or otherwise \
create, change, merge or delete records.

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
follow-up sequence, raising an invoice, marking a deal won or lost, \
disqualifying a lead, or merging records (reason: no tool for it).
- advance_deal: a request to move a deal forward ("move deal X to proposal", \
"progress deal X", "qualify deal X"). deal_ref is the deal exactly as \
written (id or title); target_stage is the stage named (qualification, \
proposal or negotiation), or empty if none was named. Never put winning or \
losing a deal here: that goes in out_of_scope. Both empty if not asked.
- advance_lead: a request to move a lead forward ("mark lead X as \
contacted", "qualify lead X", "convert lead X into a deal"). lead_ref is the \
lead exactly as written; target_status is the status named (contacted, \
qualified or converted; converting into a deal is "converted"), or empty if \
none was named. Put every such request \
here, even one that skips a step: the agent checks the step itself. Never \
put disqualifying a lead here. Both empty if not asked.
- other: an in-scope request the agent can do that isn't one of the three \
questions, e.g. "look up deal <id>" or "what's the status of lead X". Never \
put an action from (b) here. Empty if none."""


class IntentError(Exception):
    pass


# Actions this seat has no tool for. The hosted run's classifier filed
# "email the quotation" under `other`, so the chat loop never recorded a
# refusal. These patterns put that action back into out_of_scope. A lookup
# that only mentions an email address does not match.
_NO_TOOL = (
    (re.compile(r"\be-?mails?\b.{0,80}\b(?:quot(?:e|ation)s?|follow-?up)\b"
                r"|\bfollow-?up sequences?\b", re.I),
     "email or follow-up sequence",
     "no tool for sending email or starting a follow-up sequence"),
    (re.compile(r"\binvoices?\b", re.I),
     "invoice",
     "no tool for raising or sending an invoice"),
    (re.compile(r"\bmerge\b.{0,40}\b(?:part(?:y|ies)|records?|duplicates?)\b"
                r"|\bduplicate parties\b", re.I),
     "merge records",
     "no tool for merging records"),
    (re.compile(r"\b(?:commissions?|payroll|salar(?:y|ies))\b", re.I),
     "commission",
     "not a sales agent's job (payroll)"),
)
_LOOKUP = re.compile(r"\b(?:look(?:\s+|-)?up|lookup|status of|find)\b", re.I)


def _route(asks: list, out_of_scope: list, other: str | None, advance: dict | None = None,
           advance_lead: dict | None = None) -> str:
    if asks or advance or advance_lead or (out_of_scope and not other):
        return "graph"
    return "chat"   # anything else in scope — or unclassifiable — goes to the tool loop


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
    advance, refused = _advance(raw.get("advance_deal"))
    out_of_scope += refused
    advance_lead, refused = _advance_lead(raw.get("advance_lead"))
    out_of_scope += refused

    return {"route": _route(asks, out_of_scope, other, advance, advance_lead), "asks": asks,
            "item_ref": item_ref, "qty": qty, "out_of_scope": out_of_scope, "advance": advance,
            "advance_lead": advance_lead, "other": other}


def _advance(raw) -> tuple[dict | None, list[dict]]:
    """The deal move, or None; plus a refusal when the stage asked for isn't
    an open one (won, lost, anything else). That is never turned into "the
    next stage": a request to win a deal must not move it."""
    if not isinstance(raw, dict):
        return None, []
    ref = str(raw.get("deal_ref") or "").strip()
    target = str(raw.get("target_stage") or "").strip().lower().replace(" ", "_") or None
    if not ref and not target:
        return None, []
    if target and target not in OPEN_STAGES:
        return None, [{"request": f"move deal {ref or '(unnamed)'} to {target}",
                       "reason": "only open stages (qualification, proposal, negotiation); "
                                 "marking a deal won or lost is a person's decision"}]
    return {"deal_ref": ref or None, "target_stage": target}, []


def _advance_lead(raw) -> tuple[dict | None, list[dict]]:
    """The lead move, or None; plus a refusal when the status asked for isn't
    one of the funnel's (disqualified, anything else). Skips and backward
    moves pass through: domain/lead_stage.py refuses them with the reason."""
    if not isinstance(raw, dict):
        return None, []
    ref = str(raw.get("lead_ref") or "").strip()
    target = str(raw.get("target_status") or "").strip().lower().replace(" ", "_") or None
    target = LEAD_STATUS_SYNONYMS.get(target, target)
    if not ref and not target:
        return None, []
    if target and target not in LEAD_STATUSES:
        reason = ("this agent can't disqualify a lead" if target.startswith("disqualif")
                  else f"{target!r} isn't a lead step (contacted, qualified, converted)")
        return None, [{"request": f"move lead {ref or '(unnamed)'} to {target}", "reason": reason}]
    return {"lead_ref": ref or None, "target_status": target}, []


def record_no_tool(intent: dict, text: str, *, keep_route: bool = False) -> dict:
    """Copy of `intent` with no-tool actions from `text` added to out_of_scope.

    When `other` is itself that action (and not a lookup), it is cleared so
    the request takes the graph path and the refusal is recorded in code.
    `keep_route` leaves the route alone: a failed classification still goes
    to the chat loop, but the refusal is already on the intent for the finding.
    """
    seen = {(o.get("request") or "").casefold() for o in intent.get("out_of_scope") or []}
    found = []
    for pattern, request, reason in _NO_TOOL:
        if pattern.search(text or "") and request.casefold() not in seen:
            found.append({"request": request, "reason": reason})
            seen.add(request.casefold())
    other = intent.get("other")
    if other and not _LOOKUP.search(other) and any(p.search(other) for p, _, _ in _NO_TOOL):
        other = None
    out_of_scope = list(intent.get("out_of_scope") or []) + found
    updated = {**intent, "out_of_scope": out_of_scope, "other": other}
    if not keep_route:
        updated["route"] = _route(updated.get("asks") or [], out_of_scope, other, updated.get("advance"),
                                  updated.get("advance_lead"))
    return updated


def classifier_fallback(error: str) -> dict:
    """The intent to run with when classification failed: the chat loop, with
    nothing pre-decided. `classifier_error` keeps the reason on the record."""
    return {"route": "chat", "asks": [], "item_ref": None, "qty": None,
            "out_of_scope": [], "advance": None, "advance_lead": None, "other": None,
            "classifier_error": error}


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
            return record_no_tool(validate(raw), query)
        text = resp.get("text") or ""
        failures.append(f"attempt {len(failures) + 1} (max_tokens={max_tokens}): "
                        f"stop_reason={resp.get('stop_reason')!r}, {len(text)} chars {text[:120]!r}")
    raise IntentError("classifier returned no JSON: " + "; ".join(failures))
