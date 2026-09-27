#!/usr/bin/env python3
"""The harness loop: run every task, write everything to disk, score nothing.

For each task in harness/tasks.json:
  1. read the platform state the task's verifiers need (own login)
     -> runs/<run_id>/ground_truth_before.json
  2. run the agent exactly as a person would: `run.py <argv> --run-id ...`
     in a subprocess (dry run unless --write)
     -> taskrun.json, calls.jsonl, graph.json | chat_trace.json, stdout.txt
  3. read the platform state again
     -> ground_truth_after.json
and record the batch in runs/batches/<batch_id>/manifest.json.

Scoring is a separate step over those files (harness/score.py), so a
scorer fix never needs a new live run.

Usage (from agent/, so the script-style imports resolve):
    uv run python -m harness.runner [--tasks id,id] [--write] [--skip-llm]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import AGENT_DIR, REPO_DIR, RUNS_DIR, load_env
from harness import verifiers
from harness.run_record import read_json, write_json
from transport.llm_client import GLC_URL
from transport.mcp_client import client_from_env

TASKS_PATH = AGENT_DIR / "harness" / "tasks.json"
TASKS_FORMAT = "t6-tasks-v1"
AGENT_TIMEOUT_S = 900


def load_tasks(path: Path = TASKS_PATH) -> dict:
    data = read_json(path)
    if data.get("format") != TASKS_FORMAT:
        raise SystemExit(f"{path}: unsupported format {data.get('format')!r}")
    ids = [t["id"] for t in data["tasks"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"{path}: duplicate task ids {dupes}")
    for t in data["tasks"]:
        verifiers.validate_task(t)
    return data


def llm_gateway_up(timeout: float = 3.0) -> bool:
    """Any HTTP answer counts as up; only a refused/timed-out connection is down."""
    try:
        urllib.request.urlopen(GLC_URL, timeout=timeout)
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False
    return True


def run_agent(task: dict, run_id: str, today: str, write: bool, run_dir: Path) -> dict:
    cmd = [sys.executable, str(AGENT_DIR / "run.py"), *task["argv"],
           "--today", today, "--run-id", run_id, "--task-id", task["id"]]
    if not write:
        cmd.append("--dry-run")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=REPO_DIR, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=AGENT_TIMEOUT_S)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = "timeout", e.stdout or "", f"timed out after {AGENT_TIMEOUT_S}s"
    (run_dir / "stdout.txt").write_text(
        f"$ {' '.join(cmd[1:])}\n\n{out if isinstance(out, str) else out.decode('utf-8', 'replace')}"
        f"\n--- stderr ---\n{err}", encoding="utf-8")
    return {"exit_code": code, "seconds": round(time.time() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the harness task set; scoring is score.py's job.")
    ap.add_argument("--tasks", default=None, help="comma-separated task ids (default: all)")
    ap.add_argument("--write", action="store_true",
                    help="let the agent write to the platform (default: every run is --dry-run)")
    ap.add_argument("--skip-llm", action="store_true", help="skip tasks that need the LLM gateway")
    ap.add_argument("--tasks-file", type=Path, default=TASKS_PATH)
    args = ap.parse_args()

    data = load_tasks(args.tasks_file)
    tasks = data["tasks"]
    if args.tasks:
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        unknown = sorted(set(wanted) - {t["id"] for t in tasks})
        if unknown:
            ap.error(f"unknown task ids {unknown}")
        tasks = [t for t in tasks if t["id"] in wanted]

    llm_up = not args.skip_llm and llm_gateway_up()
    llm_skip_reason = ("--skip-llm" if args.skip_llm
                       else None if llm_up else f"LLM gateway unreachable at {GLC_URL}")

    batch_id = dt.datetime.now(verifiers.IST).strftime("%Y%m%dT%H%M%S")
    batch_dir = RUNS_DIR / "batches" / batch_id
    manifest = {"batch_id": batch_id, "started_at": dt.datetime.now(verifiers.IST).isoformat(),
                "write": args.write, "today": data["today"], "llm_gateway_up": llm_up,
                "tasks_file": str(args.tasks_file), "runs": []}
    write_json(batch_dir / "manifest.json", manifest)

    client = client_from_env(load_env())
    print(f"[batch {batch_id}] {len(tasks)} task(s), {'WRITE' if args.write else 'dry run'}, "
          f"today pinned to {data['today']}, LLM gateway {'up' if llm_up else 'down'}")

    for task in tasks:
        run_id = f"{batch_id}-{task['id']}"
        entry = {"task_id": task["id"], "run_id": run_id}
        if task.get("requires_llm") and llm_skip_reason:
            entry.update(status="skipped", reason=llm_skip_reason)
            manifest["runs"].append(entry)
            write_json(batch_dir / "manifest.json", manifest)
            print(f"  - {task['id']}: skipped ({llm_skip_reason})")
            continue

        run_dir = RUNS_DIR / run_id
        write_json(run_dir / "task.json", {**task, "today": data["today"]})
        keys = verifiers.observations_for(task)
        write_json(run_dir / "ground_truth_before.json", verifiers.observe_all(client, keys))
        result = run_agent(task, run_id, data["today"], args.write, run_dir)
        write_json(run_dir / "ground_truth_after.json", verifiers.observe_all(client, keys))

        entry.update(status="ran", run_dir=str(run_dir), **result)
        manifest["runs"].append(entry)
        write_json(batch_dir / "manifest.json", manifest)
        print(f"  - {task['id']}: exit {result['exit_code']} in {result['seconds']}s -> {run_dir}")

    manifest["finished_at"] = dt.datetime.now(verifiers.IST).isoformat()
    write_json(batch_dir / "manifest.json", manifest)
    print(f"\nNothing scored yet. Score with (from agent/):\n  uv run python -m harness.score {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
