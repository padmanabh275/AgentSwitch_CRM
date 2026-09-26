#!/usr/bin/env python3
"""The Sales/Pipeline agent (Team 6, seat 6) — a client, not a server.

Two paths:
  graph (default)  the composite question — "what closes this month, what
                   is at risk, and quote N units" — as a task graph whose
                   finding is computed in code (graph/, domain/).
  --chat "..."     anything else, via the LLM tool loop (llm/chat_loop.py).

Usage:
    python3 run.py [--item-id ITEM_ID] [--qty 500]
                   [--asks closing_this_month,at_risk,quote] [--today YYYY-MM-DD]
                   [--dry-run]
    python3 run.py --chat "Look up deal <id>" [--dry-run]

--today: pin the date "this month" and "overdue" are judged against, for
reproducible runs (the deal data itself is still live).

--dry-run: reads still hit live data; nothing is written (no session,
escalation or memory). Every run leaves runs/<run_id>/taskrun.json and,
for the graph path, runs/<run_id>/graph.json.

Requires EMAIL / SURYODAYA_PW in agent/.env, and glc_v5 running locally
(`uv run glc serve`) for LLM steps — the graph path still answers from its
template if the gateway is down.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import uuid

from config import RUNS_DIR, load_env
from domain.deals import IST
from graph import plans
from graph.engine import Engine
from graph.planner import RulePlanner
from graph.registry import RunContext
from harness import persist
from harness.run_record import Step, TaskRun
from llm import chat_loop
from transport.mcp_client import MCPClient, SURYODAYA


def build_query(asks: list[str], item_id: str | None, qty: int) -> str:
    parts = []
    if "closing_this_month" in asks:
        parts.append("what closes this month")
    if "at_risk" in asks:
        parts.append("what is at risk")
    if "quote" in asks:
        parts.append(f"quote {qty} units of item {item_id or '(unspecified)'} off the real BOM price")
    query = ", and ".join(parts) + "?"
    return query[0].upper() + query[1:]  # not .capitalize(): it lowercases "BOM"


def run_graph(ctx: RunContext, run: TaskRun, asks: list[str], item_id: str | None,
              qty: int) -> None:
    run_dir = RUNS_DIR / run.run_id
    engine = Engine(ctx, RulePlanner(), checkpoint_path=run_dir / "graph.json")
    store = engine.run(run.run_id, plans.pipeline_review(asks, item_id, qty))
    for spec in sorted(store.specs.values(), key=lambda s: s.started_at or float("inf")):
        o = spec.outcome
        run.steps.append(Step(
            target=spec.id, kind=spec.node, status=o.status if o else "not_run",
            reason=o.reason if o else None, error_code=o.error_code if o else None,
            attempts=spec.attempts, added_by=spec.added_by,
            seconds=round(spec.finished_at - spec.started_at, 2)
            if spec.started_at and spec.finished_at else None))
    finding = store.get("build_finding")
    narrated = store.get("narrate")
    run.finding = finding.outcome.data if finding and finding.outcome else None
    if narrated and narrated.outcome and narrated.outcome.data:
        run.answer = narrated.outcome.data.get("text")
        run.answer_source = narrated.outcome.data.get("source")
        if narrated.outcome.data.get("fallback_reason"):
            run.warnings.append(f"narrate fell back to template: {narrated.outcome.data['fallback_reason']}")


def run_chat(ctx: RunContext, run: TaskRun) -> None:
    answer, collected, trace = chat_loop.run(ctx.query, ctx.session_id, ctx.client,
                                             dry_run=ctx.dry_run, today=ctx.today)
    run.answer, run.answer_source = answer, "llm"
    run.steps = [Step(target=t["tool"], kind="tool", status=t["outcome"] or "unknown",
                      reason=t["reason"], seconds=t["seconds"]) for t in trace]
    run.finding = {**collected, "run_id": run.run_id, "session_id": run.session_id,
                   "dry_run": run.dry_run,
                   "generated_at": dt.datetime.now(IST).isoformat()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--item-id", default=None, help="Item.id to quote")
    ap.add_argument("--qty", type=int, default=500)
    ap.add_argument("--asks", default=",".join(plans.ASKS),
                    help=f"comma-separated subset of {','.join(plans.ASKS)}")
    ap.add_argument("--chat", default=None, metavar="QUESTION",
                    help="free-form question via the LLM tool loop instead of the graph")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                    help="pin 'today' for closing/at-risk (default: today on the IST calendar)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read live data but write nothing (no session, escalation or memory)")
    args = ap.parse_args()

    asks = [a.strip() for a in args.asks.split(",") if a.strip()]
    env = load_env()
    if not env.get("SURYODAYA_PW"):
        raise SystemExit("FATAL: SURYODAYA_PW is empty in agent/.env")

    run_id = dt.datetime.now(IST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    query = args.chat or build_query(asks, args.item_id, args.qty)
    run = TaskRun(run_id=run_id, task_id="chat" if args.chat else "pipeline_review",
                  prompt=query, dry_run=args.dry_run,
                  today=args.today.isoformat() if args.today else None)
    run_dir = RUNS_DIR / run_id
    t0 = time.time()

    client = MCPClient(env.get("EMAIL", ""), env["SURYODAYA_PW"], base_url=SURYODAYA)
    if not args.dry_run:
        try:
            run.session_id = persist.open_session(client)
        except Exception as e:
            run.warnings.append(f"AgentSession.create failed, running without a session "
                                f"(escalations will fail): {e}")

    print(f"[run {run_id}{' · dry run' if args.dry_run else ''}"
          f"{' · session ' + run.session_id if run.session_id else ''}] {query}\n")

    ctx = RunContext(client=client, run_id=run_id, session_id=run.session_id,
                     query=query, dry_run=args.dry_run, today=args.today)
    try:
        if args.chat:
            run_chat(ctx, run)
        else:
            run_graph(ctx, run, asks, args.item_id, args.qty)
    except Exception as e:  # record it; the run file is the evidence either way
        run.ended, run.error = "error", f"{type(e).__name__}: {e}"
    run.seconds = round(time.time() - t0, 2)
    path = run.save(run_dir)  # local record first, before any platform write

    run.warnings += persist.record_run(client, run)
    run.save(run_dir)

    print("=== answer ===")
    print(run.answer or f"(no answer — run ended with {run.ended}: {run.error})")
    print("\n=== finding (gradable) ===")
    print(json.dumps(run.finding, indent=2, default=str))
    for w in run.warnings:
        print(f"[WARN] {w}")
    print(f"\n[run record: {path}]")
    return 0 if run.ended == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
