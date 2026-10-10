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
failed as "time budget exhausted"). Keep the budget a few minutes under the
toml's timeout_minutes. SIGTERM is turned into a recorded crash.

Usage (from agent/):
    python -m harness.hosted [--tasks id,id] [--write] [--out PATH]
                             [--task-timeout S] [--budget-minutes M]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import time
import traceback
from pathlib import Path

from config import REPO_DIR
from harness import runner, score
from harness.run_record import write_json
from harness.verifiers import IST

RESULTS_PATH = REPO_DIR / "results.json"
# A full 13-task batch takes 6-8 min; the slowest task on record took 50s.
TASK_TIMEOUT_S = 180
BUDGET_MINUTES = 20
EVIDENCE_CHARS = 500
TITLE_CHARS = 200
NOT_RUN = "not run: the harness stopped before reaching this task (timeout or crash)"


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
        "evidence": evidence_for(entry, task_score)[:EVIDENCE_CHARS],
    }


def build_results(tasks: list[dict], entries: dict, scores: dict, instance: str,
                  mode: str, crash: str | None = None) -> dict:
    results = [task_result(t, entries.get(t["id"]), scores.get(t["id"]), instance)
               for t in tasks]
    if crash:
        results.append({"id": f"{instance}:harness", "title": "The harness ran to completion",
                        "passed": False, "score": 0.0, "evidence": crash[:EVIDENCE_CHARS]})
    n_pass = sum(r["passed"] for r in results)
    model = os.environ.get("OPENAI_MODEL") or "glc_v5"
    summary = (f"{instance}: {n_pass}/{len(results)} tasks passed · {mode} · model {model}"
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
                    help=f"stop starting tasks once this is nearly used (default {BUDGET_MINUTES})")
    args = ap.parse_args(argv)
    deadline = time.monotonic() + args.budget_minutes * 60

    instance = os.environ.get("AGENTSWITCH_INSTANCE") or "local"
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
    print(f"[hosted] instance {instance}, {mode}, budget {args.budget_minutes:g} min, "
          f"task timeout {args.task_timeout:g}s, results -> {args.out}", flush=True)
    try:
        data = runner.load_tasks()
        tasks = runner.select_tasks(data, args.tasks)
        save()
        runner.run_batch(data, tasks, write=args.write, on_task_done=on_task_done,
                         agent_timeout_s=args.task_timeout, deadline=deadline)
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
