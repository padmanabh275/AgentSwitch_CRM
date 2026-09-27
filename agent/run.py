#!/usr/bin/env python3
"""The Sales/Pipeline agent (Team 6, seat 6) — a client, not a server.

Three ways in:
  "a question"     free text. parse_request (LLM) classifies it; the three
                   standard questions (closing this month, at risk, quote)
                   and any out-of-scope parts go to the task graph, anything
                   else in scope goes to the chat loop.
  flags only       the graph, with the asks/item/qty given explicitly — no
                   LLM classification, so it's the reproducible path.
  --chat "..."     straight to the LLM tool loop (llm/chat_loop.py).

In the graph the finding is computed in code (graph/, domain/); the LLM
only words it, and that wording is checked against the finding.

Usage:
    python3 run.py "What closes this month? Quote 500 SuryaTools Bench Vice 150mm" [--dry-run]
    python3 run.py [--item ITEM] [--qty 500]
                   [--asks closing_this_month,at_risk,quote] [--today YYYY-MM-DD]
                   [--dry-run]
    python3 run.py --chat "Look up deal <id>" [--dry-run]

--item takes an id, a code or a name (--item-id is an alias).

--today: pin the date "this month" and "overdue" are judged against, for
reproducible runs (the deal data itself is still live).

--dry-run: reads still hit live data; nothing is written (no session,
escalation or memory). Every run leaves runs/<run_id>/taskrun.json (saved
first with ended="running"), calls.jsonl (every MCP call made), and
graph.json (graph path) or chat_trace.json (chat path).

--run-id / --task-id: set by the harness (harness/runner.py) so it knows
where the run lands and which task it was.

Requires EMAIL / SURYODAYA_PW (or a bearer TOKEN) in agent/.env or the
repo's .env, and glc_v5 running locally
(`uv run glc serve`) for LLM steps — the graph path still answers from its
template if the gateway is down.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import uuid

from config import RUNS_DIR, load_env
from domain.deals import IST
from graph import plans
from graph.engine import Engine
from graph.planner import RulePlanner
from graph.registry import RunContext
from harness import persist
from harness.call_log import LoggedClient
from harness.run_record import Step, TaskRun, write_json
from llm import chat_loop
from llm.intent import IntentError, parse_request
from transport.mcp_client import client_from_env

RUN_ID_RE = re.compile(r"[\w.-]+")


def build_query(asks: list[str], item: str | None, qty: int) -> str:
    parts = []
    if "closing_this_month" in asks:
        parts.append("what closes this month")
    if "at_risk" in asks:
        parts.append("what is at risk")
    if "quote" in asks:
        parts.append(f"quote {qty} units of {item or '(unspecified item)'} off the real BOM price")
    query = ", and ".join(parts) + "?"
    return query[0].upper() + query[1:]  # not .capitalize(): it lowercases "BOM"


def run_graph(ctx: RunContext, run: TaskRun, plan_args: dict) -> None:
    run_dir = RUNS_DIR / run.run_id
    engine = Engine(ctx, RulePlanner(), checkpoint_path=run_dir / "graph.json")
    store = engine.run(run.run_id, plans.pipeline_review(**plan_args))
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
    write_json(RUNS_DIR / run.run_id / "chat_trace.json", trace)
    run.answer, run.answer_source = answer, "llm"
    run.steps += [Step(target=t["tool"], kind="tool", status=t["outcome"] or "unknown",
                       reason=t["reason"], seconds=t["seconds"]) for t in trace]
    run.finding = {**collected, "run_id": run.run_id, "session_id": run.session_id,
                   "dry_run": run.dry_run,
                   "generated_at": dt.datetime.now(IST).isoformat()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question", nargs="?", default=None,
                    help="free-text question; classified by the LLM, then routed")
    ap.add_argument("--item", "--item-id", dest="item", default=None,
                    help="item to quote: id, code or name")
    ap.add_argument("--qty", type=int, default=500)
    ap.add_argument("--asks", default=",".join(plans.ASKS),
                    help=f"comma-separated subset of {','.join(plans.ASKS)}")
    ap.add_argument("--chat", default=None, metavar="QUESTION",
                    help="free-form question via the LLM tool loop instead of the graph")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                    help="pin 'today' for closing/at-risk (default: today on the IST calendar)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read live data but write nothing (no session, escalation or memory)")
    ap.add_argument("--run-id", default=None,
                    help="use this run id (the harness sets it); default: timestamp + random")
    ap.add_argument("--task-id", default=None,
                    help="harness task id to record on the run (tasks.json)")
    args = ap.parse_args()
    if args.question and args.chat:
        ap.error("give either a question or --chat, not both")
    if args.run_id and not RUN_ID_RE.fullmatch(args.run_id):
        ap.error("--run-id may only contain letters, digits, '.', '_' and '-'")

    asks = [a.strip() for a in args.asks.split(",") if a.strip()]
    unknown = sorted(set(asks) - set(plans.ASKS))
    if unknown:   # before login, so a typo never leaves a session behind
        ap.error(f"unknown --asks {unknown}; choose from {','.join(plans.ASKS)}")
    env = load_env()
    if not (env.get("SURYODAYA_PW") or env.get("TOKEN")):
        raise SystemExit("FATAL: no credentials — set EMAIL + SURYODAYA_PW (or TOKEN) "
                         "in agent/.env or the repo's .env")

    run_id = args.run_id or (dt.datetime.now(IST).strftime("%Y%m%dT%H%M%S")
                             + "-" + uuid.uuid4().hex[:6])
    query = args.chat or args.question or build_query(asks, args.item, args.qty)
    run = TaskRun(run_id=run_id, task_id="chat" if args.chat else "pipeline_review",
                  prompt=query, dry_run=args.dry_run,
                  today=args.today.isoformat() if args.today else None,
                  harness_task=args.task_id, ended="running")
    run_dir = RUNS_DIR / run_id
    run.save(run_dir)  # on disk from the start: a crash leaves ended="running", not nothing
    t0 = time.time()

    plan_args = {"asks": asks, "item_ref": args.item, "qty": args.qty}
    if args.question:
        # Classify before anything is written: a request we can't classify
        # shouldn't leave a session behind.
        try:
            intent = parse_request(args.question)
        except IntentError as e:
            run.ended, run.error = "error", str(e)
            run.steps.append(Step(target="parse_request", kind="llm", status="error", reason=str(e)))
            run.seconds = round(time.time() - t0, 2)
            path = run.save(run_dir)
            print(f"FATAL: {e}\nUse flags (--asks/--item/--qty) or --chat instead.\n[run record: {path}]")
            return 1
        run.intent = intent
        run.steps.append(Step(target="parse_request", kind="llm", status="answered",
                              reason=f"route={intent['route']}",
                              seconds=round(time.time() - t0, 2)))
        run.task_id = "chat" if intent["route"] == "chat" else "pipeline_review"
        plan_args = {k: intent[k] for k in ("asks", "item_ref", "qty", "out_of_scope", "other")}

    try:
        client = LoggedClient(client_from_env(env), run_dir / "calls.jsonl")
    except RuntimeError as e:  # login failed: nothing ran, but the record says why
        run.ended, run.error = "error", str(e)
        run.seconds = round(time.time() - t0, 2)
        path = run.save(run_dir)
        print(f"FATAL: {e}\n[run record: {path}]")
        return 1
    if not args.dry_run:
        try:
            run.session_id = persist.open_session(client)
        except Exception as e:
            run.warnings.append(f"AgentSession.create failed, running without a session "
                                f"(escalations will fail): {e}")

    print(f"[run {run_id}{' · dry run' if args.dry_run else ''}"
          f"{' · session ' + run.session_id if run.session_id else ''}] {query}")
    if run.intent:
        print(f"[intent] {json.dumps(run.intent, default=str)}")
    print()

    ctx = RunContext(client=client, run_id=run_id, session_id=run.session_id,
                     query=query, dry_run=args.dry_run, today=args.today)
    try:
        if run.task_id == "chat":
            run_chat(ctx, run)
        else:
            run_graph(ctx, run, plan_args)
    except Exception as e:  # record it; the run file is the evidence either way
        run.ended, run.error = "error", f"{type(e).__name__}: {e}"
    if run.ended == "running":
        run.ended = "done"
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
