"""The free-form path: an LLM tool-calling loop over the curated menu in
llm/tools.py. LLM <-> this loop <-> domain/ <-> MCP.

The composite pipeline question goes through the graph (run.py) instead,
where the finding is computed in code. This loop is for everything else —
single lookups, the refusal showcase (`get_deal` on a garbage id), and
follow-ups the fixed plan doesn't cover. Run via `run.py --chat "..."`.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from llm import tools
from llm.verify import chat_feedback, chat_template, verify_chat_answer
from transport.llm_client import call_llm
from transport.mcp_client import MCPClient

MAX_TOOL_ITERATIONS = 12

SYSTEM_PROMPT = """You are the Pipeline/Sales agent for Suryodaya Precision \
Works (seat 6 on AgentSwitch). You answer questions about deals using ONLY \
the tools provided — never estimate or guess a number a tool could have \
given you, and never invent data for an id that doesn't exist.

Definitions you must use:
- "closing this month" = list_closing_this_month's result (open-stage \
deals whose expected_close_date falls in the current IST calendar month).
- "at risk" = list_at_risk's result (open deals whose expected_close_date \
is already in the past).
- To quote a price, always call attempt_quote — never compute a price \
yourself from anything else you've read.

Escalate-vs-refuse, one test:
1. Is this legitimately your job at all? If not (e.g. payroll/commission \
questions, a discount past policy, or an id that turns out not to exist), \
refuse plainly and file nothing.
2. If yes, and you can't do it yourself but a human or another seat \
plausibly can, call file_escalation and tell the user you've escalated — \
don't guess a substitute answer.

A tool result with outcome "error" means the call failed (see error_code) \
— say so; don't treat it as "not found".

Your tools can read deals and leads, list closing/at-risk deals, quote and \
escalate. They cannot change a deal's stage, mark it won or lost, convert a \
quotation, or write anything else. When asked to do any of that, say first, \
in so many words, that you can't do it from this seat; then say what you did \
instead.

If a tool result has dry_run: true, nothing was filed: say an escalation \
would be filed, never that it was. Name an escalation number or a person only \
if a tool result contains it.

Be direct and concise. State clearly which parts you answered directly, \
which you escalated, and which you refused."""

_SECTION_FOR_TOOL = {"list_closing_this_month": "closing_this_month",
                     "list_at_risk": "at_risk", "attempt_quote": "quote"}


def run(query: str, session_id: str | None, client: MCPClient,
        dry_run: bool = False, today: dt.date | None = None
        ) -> tuple[str, dict, list[dict], dict]:
    """Returns (answer, collected finding sections, trace of tool calls, check).
    check is {"source": "llm" | "llm_rewrite" | "template", "problems": ...}.
    Raises LLMError if the gateway fails; the caller records it."""
    dispatch = tools.build_dispatch(client, session_id, dry_run=dry_run, today=today)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]
    collected: dict[str, dict] = {}
    trace: list[dict] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = call_llm(messages, tools=tools.TOOL_SPECS, tool_choice="auto")
        tool_calls = resp.get("tool_calls") or []
        if not tool_calls:
            text = resp.get("text", "")
            if resp.get("stop_reason") == "max_tokens":
                text += "\n\n(answer truncated at the token limit)"
            answer, check = _checked(query, text, messages, trace)
            return answer, collected, trace, check

        messages.append({"role": "assistant", "content": resp.get("text", ""),
                          "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("arguments", {})
            fn = dispatch.get(name)
            t0 = time.time()
            if fn is None:
                result = {"outcome": "error", "reason": f"unknown tool {name}"}
            else:
                try:
                    result = fn(args)
                except Exception as e:  # a tool call this seat is allowed to make still failed
                    result = {"outcome": "error", "error_code": getattr(e, "code", None),
                              "reason": str(e)}
            trace.append({"tool": name, "arguments": args, "outcome": result.get("outcome"),
                          "reason": result.get("reason"), "seconds": round(time.time() - t0, 2),
                          "result": result})
            if name in _SECTION_FOR_TOOL:
                collected[_SECTION_FOR_TOOL[name]] = result
            messages.append({
                "role": "tool", "tool_call_id": tc["id"], "name": name,
                "content": json.dumps(result, default=str),
            })

    return ("(stopped after max tool iterations without a final answer)", collected, trace,
            {"source": "none", "problems": None})


def _checked(query: str, text: str, messages: list[dict], trace: list[dict]) -> tuple[str, dict]:
    """The answer only if it matches the trace: else one rewrite, else the template."""
    first = verify_chat_answer(text, query, trace)
    if first["ok"]:
        return text, {"source": "llm", "problems": None}
    retry = messages + [{"role": "assistant", "content": text},
                        {"role": "user", "content": chat_feedback(first)}]
    rewritten = call_llm(retry, tools=None).get("text", "")
    second = verify_chat_answer(rewritten, query, trace)
    if second["ok"]:
        return rewritten, {"source": "llm_rewrite", "problems": first}
    return chat_template(trace), {"source": "template", "problems": second}
