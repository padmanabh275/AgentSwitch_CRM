#!/usr/bin/env python3
"""The Sales/Pipeline agent loop (Team 6, seat 6) — a client loop, not a
server. LLM <-> agent.py (this file) <-> domain.py <-> MCP/REST.

Usage:
    python3 agent.py [--item-id ITEM_ID] [--qty 500] [--query "..."]

Requires glc_v5 running locally (`uv run glc serve`, see llm_client.py) and
EMAIL / SURYODAYA_PW in .env (see mcp_client.py).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import tools
from llm_client import LLMError, call_llm
from mcp_client import MCPClient, SURYODAYA

MAX_TOOL_ITERATIONS = 12

SYSTEM_PROMPT = """You are the Pipeline/Sales agent for Suryodaya Precision \
Works (seat 6 on AgentSwitch). You answer questions about deals using ONLY \
the tools provided — never estimate or guess a number a tool could have \
given you, and never invent data for an id that doesn't exist.

Definitions you must use:
- "closing this month" = list_closing_this_month's result (open-stage \
deals whose expected_close_date falls in the current calendar month).
- "at risk" = list_at_risk's result (open deals whose expected_close_date \
is already in the past).
- To quote a price, always call attempt_quote — never compute a price \
yourself from anything else you've read.

Escalate-vs-refuse, one test:
1. Is this legitimately your job at all? If not (e.g. payroll/commission \
questions, another rep's private data, a discount past policy, or an id \
that turns out not to exist), refuse plainly and file nothing.
2. If yes, and you can't do it yourself but a human or another seat \
plausibly can, call file_escalation and tell the user you've escalated — \
don't guess a substitute answer.

Be direct and concise. State clearly which parts you answered directly, \
which you escalated, and which you refused."""

DEFAULT_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path: str = DEFAULT_ENV_PATH) -> dict:
    env = dict(os.environ)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    return env


def build_query(item_id: str | None, qty: int) -> str:
    base = "What closes this month, and what is at risk?"
    if item_id:
        return base + f" Also quote {qty} units of item {item_id} off the real BOM price."
    return base


def run(query: str, session_id: str, client: MCPClient) -> tuple[str, dict]:
    dispatch = tools.build_dispatch(client, session_id)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]
    collected: dict[str, dict] = {}

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = call_llm(messages, tools=tools.TOOL_SPECS, tool_choice="auto")
        tool_calls = resp.get("tool_calls") or []
        if not tool_calls:
            return resp.get("text", ""), collected

        messages.append({"role": "assistant", "content": resp.get("text", ""),
                          "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc["name"]
            fn = dispatch.get(name)
            if fn is None:
                result = {"outcome": "error", "reason": f"unknown tool {name}"}
            else:
                try:
                    result = fn(tc.get("arguments", {}))
                except Exception as e:  # a tool call this seat is allowed to make still failed
                    result = {"outcome": "error", "reason": str(e)}
            if name in ("list_closing_this_month", "list_at_risk"):
                collected[{"list_closing_this_month": "closing_this_month",
                           "list_at_risk": "at_risk"}[name]] = result
            elif name == "attempt_quote":
                collected["quote"] = result
            messages.append({
                "role": "tool", "tool_call_id": tc["id"], "name": name,
                "content": json.dumps(result),
            })

    return "(stopped after max tool iterations without a final answer)", collected


def build_finding(collected: dict) -> dict:
    finding = dict(collected)
    finding["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return finding


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--item-id", default=None, help="Item.id to quote (omit to skip the quote question)")
    ap.add_argument("--qty", type=int, default=500)
    ap.add_argument("--query", default=None, help="override the default composite question")
    args = ap.parse_args()

    env = load_env()
    email = env.get("EMAIL", "")
    pw = env.get("SURYODAYA_PW", "")
    if not pw:
        raise SystemExit("FATAL: SURYODAYA_PW is empty in .env")

    client = MCPClient(email, pw, base_url=SURYODAYA)
    # session_id is a foreign key to a real AgentSession, not a free-form
    # string — AgentMemory.create and AgentEscalation.create both reject a
    # bare uuid4 with "Referenced AgentSession does not exist" (found live).
    session = client.call("AgentSession.create", {
        "channel": "api", "title": "Sales pipeline agent run",
    })
    session_id = session["id"]
    query = args.query or build_query(args.item_id, args.qty)

    print(f"[session {session_id}] query: {query}\n")
    try:
        answer, collected = run(query, session_id, client)
    except LLMError as e:
        raise SystemExit(f"FATAL: {e}")

    print("=== answer ===")
    print(answer)

    finding = build_finding(collected)
    print("\n=== finding (gradable) ===")
    print(json.dumps(finding, indent=2))

    try:
        client.call("AgentMemory.create", {
            "session_id": session_id,
            "category": "context",
            "content": json.dumps(finding),
        })
        print("\n[stored finding via AgentMemory.create]")
    except Exception as e:
        print(f"\n[WARN: could not store finding via AgentMemory.create: {e}]")


if __name__ == "__main__":
    main()
