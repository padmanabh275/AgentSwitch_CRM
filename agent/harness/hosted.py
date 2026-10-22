#!/usr/bin/env python3
"""Entry point for the hosted AgentSwitch harness run ("Our harness" → Submit).

Runs the task set (runner.run_batch), scores each task the moment it
finishes (score.score_run), and keeps <repo>/results.json in the platform's
format up to date throughout:

    {"tasks": [{"id", "title", "passed", "score", "evidence"}], "summary": "..."}

The file is written before the first task starts and rewritten after every
task, so a crash or the platform's timeout still leaves a valid file: tasks
that never ran are listed as failed with that reason. The full evidence
(runs/<run_id>/, runs/batches/<batch_id>/) is written as usual.

Time: each agent run is killed after --task-timeout seconds, and the batch
stops starting tasks when --budget-minutes is nearly used up (the rest are
failed as "time budget exhausted"). The toml's timeout_minutes is one clock
shared by all instances: the first instance writes its start time to
hosted_clock.json beside the results file, and each later one stops at
start + --total-minutes. Keep --total-minutes a few minutes under
timeout_minutes. Tasks
marked hosted_repeat_of in tasks.json are left out unless named with --tasks. A spent *token* budget (HTTP 429 budget_exhausted)
also stops the batch: the rest are skipped with that reason and the summary
says so. SIGTERM is turned into a recorded crash.

Usage (from agent/):
    python -m harness.hosted [--tasks id,id] [--write] [--out PATH]
                             [--task-timeout S] [--budget-minutes M]
                             [--total-minutes M]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import time
import traceback
from pathlib import Path

from config import REPO_DIR, load_env
from harness import fixtures, runner, score
from harness.run_record import read_json, write_json
from harness.verifiers import IST
from transport.mcp_client import client_from_env

RESULTS_PATH = REPO_DIR / "results.json"
# Beside the results file: when the first instance started, so later ones
# share its clock (see shared_deadline).
CLOCK_NAME = "hosted_clock.json"
# The platform's timeout_minutes is one clock for install plus every instance
# in turn. A 25-task write batch took 14 min on suryodaya (~8 min agent, ~6
# min fixtures and scoring); the 16 left after omit_repeats take ~9. The
# per-instance budget keeps the first instance from starving the second; the
# total is the shared clock, a few minutes under timeout_minutes.
# The slowest task on record took 57s.
TASK_TIMEOUT_S = 180
BUDGET_MINUTES = 13.5
TOTAL_MINUTES = 27
EVIDENCE_CHARS = 500
TITLE_CHARS = 200
NOT_RUN = "not run: the harness stopped before reaching this task (timeout or crash)"
# Credentials the evaluator injects. Evidence is what the platform stores.
SECRET_ENV = ("AGENTSWITCH_TOKEN", "OPENAI_API_KEY")


def scrub(text: str) -> str:
    """Replace evaluator credentials if a failure message echoed them."""
    for name in SECRET_ENV:
        value = os.environ.get(name) or ""
        if len(value) >= 8:
            text = text.replace(value, f"<{name}>")
    return text


def omit_blocked_priced_tasks(tasks: list[dict], priced_available: bool) -> list[dict]:
    """Priced-item write tasks error in the fixture while standard_rate is
    redacted. Leaving them in results.json only adds failures. An explicit
    --tasks list is not passed through here, so a named task can still be seen."""
    if priced_available:
        return tasks
    return [t for t in tasks if t.get("fixture") != "open_deal_priced_item"]


def shared_deadline(clock_path: Path, instance: str, total_minutes: float,
                    now: float) -> float:
    """Wall-clock (epoch) deadline for the whole submission. The first instance
    writes its start time to clock_path; a later instance of the same
    submission counts from it. A clock that is unreadable, older than
    total_minutes, or already lists this instance is from an earlier
    submission (or a local run), so this run starts a new one."""
    try:
        clock = read_json(clock_path)
        started = float(clock["started"])
        seen = list(clock["instances"])
    except (OSError, ValueError, KeyError, TypeError):
        started, seen = now, []
    if not 0 <= now - started < total_minutes * 60 or instance in seen:
        started, seen = now, []
    write_json(clock_path, {"started": started, "instances": [*seen, instance]})
    return started + total_minutes * 60


def omit_repeats(tasks: list[dict]) -> list[dict]:
    """Tasks marked hosted_repeat_of cover the same behaviour as the task they
    name; the hosted run leaves them out to fit two instances in the timeout.
    An explicit --tasks list is not passed through here."""
    return [t for t in tasks if not t.get("hosted_repeat_of")]


def check_score(task_score: dict) -> float:
    """Fraction of the task's checks that passed; skipped checks don't count."""
    counted = [c for c in task_score.get("checks", []) if c.get("status") != "skip"]
    if not counted:
        return 1.0 if task_score.get("status") == "pass" else 0.0
    return round(sum(c["status"] == "pass" for c in counted) / len(counted), 3)


def evidence_for(entry: dict | None, task_score: dict | None) -> str:
    if entry is None:
        return NOT_RUN
    if task_score is None:
        if entry.get("status") == "ran":
            return f"no score: {entry.get('score_error') or 'no taskrun.json was written'} " \
                   f"(agent exit {entry.get('exit_code')}, {entry.get('seconds')}s)"
        return f"{entry.get('status')}: {entry.get('reason', '')}"
    counted = [c for c in task_score["checks"] if c["status"] != "skip"]
    passed = sum(c["status"] == "pass" for c in counted)
    head = f"{task_score['status']}: {passed}/{len(counted)} checks pass ({entry.get('seconds')}s)"
    if entry.get("exit_code") == "timeout":
        head = f"agent killed after the {entry.get('seconds')}s timeout; {head}"
    problems = [f"{c['verifier']}/{c['name']} {c['status']}: {c['detail']}"
                for c in task_score["checks"] if c["status"] not in ("pass", "skip")]
    return "; ".join([head, *problems])


def task_result(task: dict, entry: dict | None, task_score: dict | None, instance: str) -> dict:
    passed = bool(task_score and task_score.get("status") == "pass")
    return {
        "id": f"{instance}:{task['id']}",
        "title": (task.get("description") or task["id"])[:TITLE_CHARS],
        "passed": passed,
        "score": check_score(task_score) if task_score else 0.0,
        "evidence": scrub(evidence_for(entry, task_score))[:EVIDENCE_CHARS],
    }


def build_results(tasks: list[dict], entries: dict, scores: dict, instance: str,
                  mode: str, crash: str | None = None) -> dict:
    results = [task_result(t, entries.get(t["id"]), scores.get(t["id"]), instance)
               for t in tasks]
    if crash:
        results.append({"id": f"{instance}:harness", "title": "The harness ran to completion",
                        "passed": False, "score": 0.0,
                        "evidence": scrub(crash)[:EVIDENCE_CHARS]})
    n_pass = sum(r["passed"] for r in results)
    model = os.environ.get("OPENAI_MODEL") or "glc_v5"
    budget = any(e.get("budget_exhausted") for e in entries.values())
    summary = (f"{instance}: {n_pass}/{len(results)} tasks passed · {mode} · model {model}"
               + (" · token budget exhausted (429)" if budget else "")
               + (" · harness crashed" if crash else ""))
    return {"tasks": results, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hosted harness run: run, score, write results.json.")
    ap.add_argument("--tasks", default=None, help="comma-separated task ids (default: all)")
    ap.add_argument("--write", action="store_true",
                    help="let the agent write (the hosted instance is a throwaway copy)")
    ap.add_argument("--out", type=Path, default=RESULTS_PATH, help="results file to write")
    ap.add_argument("--task-timeout", type=float, default=TASK_TIMEOUT_S,
                    help=f"seconds before one agent run is killed (default {TASK_TIMEOUT_S})")
    ap.add_argument("--budget-minutes", type=float, default=BUDGET_MINUTES,
                    help=f"per instance: stop starting tasks once this is nearly used "
                         f"(default {BUDGET_MINUTES})")
    ap.add_argument("--total-minutes", type=float, default=TOTAL_MINUTES,
                    help=f"whole submission, shared by all instances through {CLOCK_NAME} "
                         f"beside --out (default {TOTAL_MINUTES})")
    args = ap.parse_args(argv)
    instance = os.environ.get("AGENTSWITCH_INSTANCE") or "local"
    total_left_s = shared_deadline(args.out.parent / CLOCK_NAME, instance,
                                   args.total_minutes, time.time()) - time.time()
    deadline = time.monotonic() + min(args.budget_minutes * 60, total_left_s)
    mode = "write" if args.write else "dry run"
    tasks: list[dict] = []
    entries: dict[str, dict] = {}
    scores: dict[str, dict] = {}

    def save(crash: str | None = None) -> None:
        write_json(args.out, build_results(tasks, entries, scores, instance, mode, crash))

    def on_task_done(entry: dict) -> None:
        if entry.get("status") == "ran":
            run_dir = Path(entry["run_dir"])
            if (run_dir / "taskrun.json").exists():
                try:
                    scores[entry["task_id"]] = score.score_run(run_dir)
                except Exception as e:  # a scorer bug fails this task, not the file
                    entry["score_error"] = f"scoring failed: {type(e).__name__}: {e}"
        entries[entry["task_id"]] = entry
        save()

    def on_sigterm(signum, frame):
        raise SystemExit("terminated by SIGTERM (platform timeout?)")

    signal.signal(signal.SIGTERM, on_sigterm)
    print(f"[hosted] instance {instance}, {mode}, budget {args.budget_minutes:g} min "
          f"({total_left_s / 60:.1f} of {args.total_minutes:g} min left on the shared clock), "
          f"task timeout {args.task_timeout:g}s, results -> {args.out}", flush=True)
    try:
        data = runner.load_tasks()
        tasks = runner.select_tasks(data, args.tasks)
        if not args.tasks:
            tasks = omit_repeats(tasks)
        if not args.write and not args.tasks:
            # Write tasks can't pass a dry run; listing them would only add
            # failures to results.json. Name one with --tasks to see it skipped.
            tasks = [t for t in tasks if not t.get("requires_write")]
        elif args.write and not args.tasks and any(
                t.get("fixture") == "open_deal_priced_item" for t in tasks):
            priced = fixtures.priced_item_available(client_from_env(load_env()))
            before = len(tasks)
            tasks = omit_blocked_priced_tasks(tasks, priced)
            if len(tasks) != before:
                print("[hosted] leaving out priced-item write tasks: "
                      "no sellable item with a readable standard_rate", flush=True)
        save()
        # The hosted instance is a throwaway copy: records the seat can't
        # remove (sales orders) are fine there.
        runner.run_batch(data, tasks, write=args.write, on_task_done=on_task_done,
                         agent_timeout_s=args.task_timeout, deadline=deadline, allow_permanent=True)
    except BaseException as e:  # incl. KeyboardInterrupt/SystemExit: still leave a valid file
        crash = f"{type(e).__name__}: {e} | {traceback.format_exc(limit=3)[-300:]}"
        save(crash)
        print(f"[hosted] harness crashed: {crash}", flush=True)
        if not isinstance(e, Exception):
            raise
        return 0   # results.json is valid; let the platform read it
    save()
    n_pass = sum(s.get("status") == "pass" for s in scores.values())
    print(f"[hosted] {n_pass}/{len(tasks)} tasks passed at "
          f"{dt.datetime.now(IST).isoformat(timespec='seconds')} -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
