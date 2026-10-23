#!/usr/bin/env python3
"""The harness loop: run every task, write everything to disk, score nothing.

For each task in harness/tasks.json:
  0. a write task with a "fixture" gets its own fixture rows first
     (harness/fixtures.py) -> runs/<run_id>/fixture.json; `{deal_id}`-style
     placeholders in its argv and observation keys are filled from it
  1. read the platform state the task's verifiers need (own login)
     -> runs/<run_id>/ground_truth_before.json
  2. run the agent exactly as a person would: `run.py <argv> --run-id ...`
     in a subprocess (dry run unless --write)
     -> taskrun.json, calls.jsonl, graph.json | chat_trace.json, stdout.txt
     With "interference", the agent is held at harness/gate.py's gate
     while the runner makes another writer's change
     -> interference.json, on disk before the agent is released
  3. read the platform state again
     -> ground_truth_after.json
  4. retire the fixture (a still-open fixture deal is marked lost)
A task in "steps" (a funnel) runs steps 1-3 once per step instead, each
agent step as its own run, runs/<run_id>.<n>-<step>, with the step's own
task.json and fixture.json (the funnel's state so far) so the existing
verifiers score it. A step's "capture" adds values from its finding to the
state (the deal a convert made, say); a "harness" step is a person's
hand-off (fixtures.handoff). The first step that fails stops the funnel.
The task's own run_dir keeps the order in steps.json, and its own
verifiers look at the whole chain before the fixture is retired.
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
from harness import fixtures, gate, task_keys, verifiers
from harness.batch_client import BatchClient
from harness.run_record import read_json, write_json
from transport.llm_client import backend_url
from transport.mcp_client import BUDGET_EXHAUSTED, client_from_env, instance_from_env

TASKS_PATH = AGENT_DIR / "harness" / "tasks.json"
TASKS_FORMAT = "t6-tasks-v1"
AGENT_TIMEOUT_S = 900
# With a deadline: what a task needs besides the agent (the after-read and
# scoring), and the least agent time worth starting a task with.
TASK_RESERVE_S = 60
MIN_AGENT_S = 60
# A spent token budget is HTTP 429 budget_exhausted (not 401), and it doesn't
# come back within the batch: every later task would fail the same way.
BUDGET_REASON = "token budget exhausted (HTTP 429 budget_exhausted) before this task started"
# A task that leaves records the seat can't remove (a SalesOrder has no
# delete or cancel) runs only on a throwaway instance: the hosted run, or a
# local batch given --allow-permanent.
PERMANENT_REASON = ("leaves records this seat can't remove (a sales order): runs on the hosted "
                    "instance, or locally with --allow-permanent")
BUDGET_EVIDENCE = ("taskrun.json", "calls.jsonl", "chat_trace.json", "graph.json")


def budget_exhausted_in(run_dir: Path) -> bool:
    """Whether the run's own records show a 429 budget_exhausted, from the
    platform (mcp_client) or the model (llm_client)."""
    for name in BUDGET_EVIDENCE:
        p = run_dir / name
        try:
            if p.exists() and BUDGET_EXHAUSTED in p.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def for_instance(task: dict, instance: str | None) -> dict:
    """The task as it runs on `instance`: its by_instance[instance] fields
    (e.g. an item ref that exists there) replace the defaults, which are
    written for Suryodaya. by_instance itself is dropped; the instance is kept."""
    base = {k: v for k, v in task.items() if k != "by_instance"}
    override = (task.get("by_instance") or {}).get(instance) or {}
    return {**base, **override, "instance": instance}


def load_tasks(path: Path = TASKS_PATH, instance: str | None = None) -> dict:
    """The task set, resolved for `instance` (default: the one the env's
    credentials point at, see instance_from_env)."""
    data = read_json(path)
    if data.get("format") != TASKS_FORMAT:
        raise SystemExit(f"{path}: unsupported format {data.get('format')!r}")
    task_keys.check_file_keys(path, data)
    ids = [t["id"] for t in data["tasks"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"{path}: duplicate task ids {dupes}")
    for t in data["tasks"]:
        verifiers.validate_task(t)
    instance = instance or instance_from_env(load_env())
    return {**data, "instance": instance,
            "tasks": [for_instance(t, instance) for t in data["tasks"]]}


def llm_gateway_up(timeout: float = 3.0) -> bool:
    """Any HTTP answer counts as up; only a refused/timed-out connection is down."""
    try:
        urllib.request.urlopen(backend_url(), timeout=timeout)
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False
    return True


POLL_S = 0.2


def run_agent(task: dict, run_id: str, today: str, write: bool, run_dir: Path,
              timeout_s: float = AGENT_TIMEOUT_S, argv: list[str] | None = None,
              on_gate=None, gate_name: str = "before_quotation_write") -> dict:
    """Run the agent in a subprocess. With on_gate, the agent is told to
    stop at gate_name (harness/gate.py); on_gate() runs while it waits,
    and the agent is released only after it returns."""
    cmd = [sys.executable, str(AGENT_DIR / "run.py"), *(argv or task["argv"]),
           "--today", today, "--run-id", run_id, "--task-id", task["id"]]
    if not write:
        cmd.append("--dry-run")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop(gate.ENV, None)
    gate_dir = run_dir / "gate"
    if on_gate:
        env[gate.ENV] = str(gate_dir)
    out_path, err_path = run_dir / "stdout.raw", run_dir / "stderr.raw"
    t0 = time.time()
    gated = False
    with out_path.open("w", encoding="utf-8") as out_f, err_path.open("w", encoding="utf-8") as err_f:
        proc = subprocess.Popen(cmd, cwd=REPO_DIR, env=env, stdout=out_f, stderr=err_f,
                                text=True, encoding="utf-8", errors="replace")
        code: int | str | None = None
        while code is None:
            code = proc.poll()
            if code is not None:
                break
            if time.time() - t0 > timeout_s:
                proc.kill()
                proc.wait()
                code = "timeout"
                break
            if on_gate and not gated and (gate_dir / f"{gate_name}.ready").exists():
                gated = True
                try:
                    on_gate()
                finally:  # never leave the agent hanging on a failed interference
                    (gate_dir / f"{gate_name}.go").write_text("go", encoding="utf-8")
            time.sleep(POLL_S)
    out = out_path.read_text(encoding="utf-8", errors="replace")
    err = err_path.read_text(encoding="utf-8", errors="replace")
    if code == "timeout":
        err += f"\ntimed out after {timeout_s:.0f}s"
    out_path.unlink(missing_ok=True)
    err_path.unlink(missing_ok=True)
    (run_dir / "stdout.txt").write_text(
        f"$ {' '.join(cmd[1:])}\n\n{out}\n--- stderr ---\n{err}", encoding="utf-8")
    result = {"exit_code": code, "seconds": round(time.time() - t0, 2)}
    if on_gate:
        result["gate_reached"] = gated
    return result


def run_task(client, task: dict, run_id: str, today: str, write: bool, run_dir: Path,
             timeout_s: float) -> dict:
    """Fixture, ground truth before, the agent (with any interference),
    ground truth after, fixture retired. Every step is on disk as it happens."""
    if task.get("steps"):
        return run_steps(client, task, run_id, today, write, run_dir, timeout_s)
    write_json(run_dir / "task.json", {**task, "today": today})
    fixture = None
    if task.get("fixture"):
        fixture = fixtures.create(client, task["fixture"], run_id)
        write_json(run_dir / "fixture.json", fixture)
    extra: dict = {}   # only what this task needs, so run_agent's plain call shape is unchanged
    if fixture:
        extra["argv"] = [verifiers.resolve(a, fixture) for a in task["argv"]]
        write_json(run_dir / "task.json", {**task, "today": today, "argv_resolved": extra["argv"]})
    keys = [verifiers.resolve(k, fixture) for k in verifiers.observations_for(task)]
    write_json(run_dir / "ground_truth_before.json", verifiers.observe_all(client, keys))

    if task.get("interference"):
        def on_gate():
            try:
                done = fixtures.interfere(client, task["interference"], fixture)
            except Exception as e:
                done = {"kind": task["interference"], "at": time.time(),
                        "error": f"{type(e).__name__}: {e}"[:300]}
            write_json(run_dir / "interference.json", done)  # before the agent is released
        extra["on_gate"] = on_gate
        extra["gate_name"] = fixtures.INTERFERENCE_GATES[task["interference"]]
    result = run_agent(task, run_id, today, write, run_dir, timeout_s, **extra)
    write_json(run_dir / "ground_truth_after.json", verifiers.observe_all(client, keys))
    if fixture:
        write_json(run_dir / "fixture.json", {**fixture, "retired": fixtures.retire(client, fixture)})
    return result


def _dig(finding: dict, path: str):
    """finding["a"]["b"] for "a.b", or None."""
    value = finding
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _resolvable(keys: list[str], state: dict) -> list[str]:
    """The observation keys whose placeholders the state can fill yet."""
    resolved = [verifiers.resolve(k, state) for k in keys]
    return [k for k in resolved if not verifiers.PLACEHOLDER.search(k)]


def run_steps(client, task: dict, run_id: str, today: str, write: bool, run_dir: Path,
              timeout_s: float) -> dict:
    """A funnel: one fixture, then each step in order (see the module
    docstring), all inside timeout_s; the fixture is retired at the end."""
    t0 = time.time()
    deadline = time.monotonic() + timeout_s
    write_json(run_dir / "task.json", {**task, "today": today})
    state = fixtures.create(client, task["fixture"], run_id)
    write_json(run_dir / "fixture.json", state)
    keys = verifiers.observations_for(task)
    write_json(run_dir / "ground_truth_before.json",
               verifiers.observe_all(client, _resolvable(keys, state)))
    log: list[dict] = []
    stopped = None
    try:
        for n, step in enumerate(task["steps"], 1):
            entry = {"step": step["name"]}
            if stopped:
                log.append({**entry, "status": "not_run", "reason": stopped})
                continue
            if step.get("harness"):
                try:
                    done = fixtures.handoff(client, step["harness"], state)
                    state.update(done.pop("state", {}))
                    entry.update(status="done", **done)
                except Exception as e:
                    entry.update(status="error", reason=f"{type(e).__name__}: {e}"[:300])
                    stopped = f"hand-off {step['name']} failed"
                log.append(entry)
                write_json(run_dir / "steps.json", log)
                continue
            left_s = deadline - time.monotonic()
            if left_s < min(MIN_AGENT_S, timeout_s):
                stopped = "time budget exhausted"
                log.append({**entry, "status": "not_run", "reason": stopped})
                continue
            sub_id = f"{run_id}.{n}-{step['name']}"
            sub_dir = RUNS_DIR / sub_id
            sub_task = verifiers.step_task(task, step)
            argv = [verifiers.resolve(a, state) for a in step["argv"]]
            write_json(sub_dir / "task.json", {**sub_task, "today": today, "argv_resolved": argv})
            write_json(sub_dir / "fixture.json", state)
            sub_keys = [verifiers.resolve(k, state) for k in verifiers.observations_for(sub_task)]
            write_json(sub_dir / "ground_truth_before.json", verifiers.observe_all(client, sub_keys))
            result = run_agent(sub_task, sub_id, today, write, sub_dir, left_s, argv=argv)
            write_json(sub_dir / "ground_truth_after.json", verifiers.observe_all(client, sub_keys))
            entry.update(status="ran", run_id=sub_id, run_dir=str(sub_dir), **result)
            taskrun = read_json(sub_dir / "taskrun.json") if (sub_dir / "taskrun.json").exists() else {}
            captured = {k: _dig(taskrun.get("finding") or {}, path)
                        for k, path in (step.get("capture") or {}).items()}
            entry["captured"] = captured
            missing = sorted(k for k, v in captured.items() if v is None)
            if result["exit_code"] != 0:
                stopped = f"step {step['name']} exited {result['exit_code']}"
            elif missing:
                stopped = f"step {step['name']}'s finding has no {', '.join(missing)}"
            state.update({k: v for k, v in captured.items() if v is not None})
            log.append(entry)
            write_json(run_dir / "steps.json", log)
            write_json(run_dir / "fixture.json", state)
    finally:
        write_json(run_dir / "steps.json", log)
        write_json(run_dir / "ground_truth_after.json",
                   verifiers.observe_all(client, _resolvable(keys, state)))
        write_json(run_dir / "fixture.json", {**state, "retired": fixtures.retire(client, state)})
    return {"exit_code": 1 if stopped else 0, "seconds": round(time.time() - t0, 2),
            "steps": len(log), **({"stopped": stopped} if stopped else {})}


def select_tasks(data: dict, task_ids: str | None) -> list[dict]:
    """The tasks to run, in file order. Raises ValueError on an unknown id."""
    tasks = data["tasks"]
    if task_ids:
        wanted = [t.strip() for t in task_ids.split(",") if t.strip()]
        unknown = sorted(set(wanted) - {t["id"] for t in tasks})
        if unknown:
            raise ValueError(f"unknown task ids {unknown}")
        tasks = [t for t in tasks if t["id"] in wanted]
    return tasks


def run_batch(data: dict, tasks: list[dict], write: bool = False, skip_llm: bool = False,
              tasks_file: Path = TASKS_PATH, on_task_done=None,
              agent_timeout_s: float = AGENT_TIMEOUT_S, deadline: float | None = None,
              allow_permanent: bool = False) -> Path:
    """Run `tasks` and record the batch; returns the batch directory.

    Each agent run is killed after agent_timeout_s. With a `deadline` (a
    time.monotonic() value) the agent also gets no more than the time left
    minus TASK_RESERVE_S, and once that is under MIN_AGENT_S the remaining
    tasks are recorded as skipped ("time budget exhausted") instead of run.

    on_task_done(entry), if given, is called after each task's manifest entry
    is saved — the hosted run uses it to rewrite results.json as it goes. A
    task whose ground-truth read or agent launch raises is recorded as
    status "error" and the batch carries on. Once a task hits a spent token
    budget (429 budget_exhausted), the remaining tasks are skipped with
    BUDGET_REASON instead of run.
    """
    llm_up = not skip_llm and llm_gateway_up()
    llm_skip_reason = ("--skip-llm" if skip_llm
                       else None if llm_up else f"LLM backend unreachable at {backend_url()}")

    batch_id = dt.datetime.now(verifiers.IST).strftime("%Y%m%dT%H%M%S")
    batch_dir = RUNS_DIR / "batches" / batch_id
    manifest = {"batch_id": batch_id, "started_at": dt.datetime.now(verifiers.IST).isoformat(),
                "instance": data.get("instance"),
                "write": write, "today": data["today"], "llm_gateway_up": llm_up,
                "tasks_file": str(tasks_file), "runs": []}
    write_json(batch_dir / "manifest.json", manifest)

    client = BatchClient(client_from_env(load_env()))
    print(f"[batch {batch_id}] {data.get('instance') or 'unknown instance'}, "
          f"{len(tasks)} task(s), {'WRITE' if write else 'dry run'}, "
          f"today pinned to {data['today']}, LLM gateway {'up' if llm_up else 'down'}", flush=True)

    budget_spent = False
    for task in tasks:
        run_id = f"{batch_id}-{task['id']}"
        entry = {"task_id": task["id"], "run_id": run_id}
        # Agent time the deadline still allows; skip only when *that* runs short.
        left_s = (float("inf") if deadline is None
                  else deadline - time.monotonic() - TASK_RESERVE_S)
        timeout_s = min(agent_timeout_s, left_s)
        if task.get("requires_write") and not write:
            entry.update(status="skipped", reason="write task: needs a --write batch")
            print(f"  - {task['id']}: skipped (write task, dry batch)", flush=True)
        elif task.get("requires_llm") and llm_skip_reason:
            entry.update(status="skipped", reason=llm_skip_reason)
            print(f"  - {task['id']}: skipped ({llm_skip_reason})", flush=True)
        elif task.get("permanent_records") and not allow_permanent:
            entry.update(status="skipped", reason=PERMANENT_REASON)
            print(f"  - {task['id']}: skipped (permanent records; --allow-permanent)", flush=True)
        elif budget_spent:
            entry.update(status="skipped", reason=BUDGET_REASON)
            print(f"  - {task['id']}: skipped (token budget exhausted)", flush=True)
        elif left_s < min(MIN_AGENT_S, agent_timeout_s):
            entry.update(status="skipped", reason="time budget exhausted before this task started")
            print(f"  - {task['id']}: skipped (time budget exhausted)", flush=True)
        else:
            run_dir = RUNS_DIR / run_id
            try:
                result = run_task(client, task, run_id, data["today"], write, run_dir, timeout_s)
            except Exception as e:  # one broken task must not cost the rest of the batch
                entry.update(status="error", run_dir=str(run_dir),
                             reason=f"{type(e).__name__}: {e}"[:500])
                print(f"  - {task['id']}: error ({entry['reason']})", flush=True)
                budget_spent = BUDGET_EXHAUSTED in f"{getattr(e, 'code', '')} {e}"
            else:
                entry.update(status="ran", run_dir=str(run_dir), **result)
                print(f"  - {task['id']}: exit {result['exit_code']} in {result['seconds']}s "
                      f"-> {run_dir}", flush=True)
                budget_spent = budget_exhausted_in(run_dir)
            if budget_spent:
                entry["budget_exhausted"] = True
                print(f"  ! {task['id']} hit 429 budget_exhausted: skipping the rest", flush=True)
        manifest["runs"].append(entry)
        write_json(batch_dir / "manifest.json", manifest)
        if on_task_done:
            on_task_done(entry)

    manifest["finished_at"] = dt.datetime.now(verifiers.IST).isoformat()
    write_json(batch_dir / "manifest.json", manifest)
    return batch_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the harness task set; scoring is score.py's job.")
    ap.add_argument("--tasks", default=None, help="comma-separated task ids (default: all)")
    ap.add_argument("--write", action="store_true",
                    help="let the agent write to the platform (default: every run is --dry-run)")
    ap.add_argument("--skip-llm", action="store_true", help="skip tasks that need the LLM gateway")
    ap.add_argument("--tasks-file", type=Path, default=TASKS_PATH)
    ap.add_argument("--allow-permanent", action="store_true",
                    help="also run tasks that leave records the seat can't remove (sales orders)")
    args = ap.parse_args()

    data = load_tasks(args.tasks_file)
    try:
        tasks = select_tasks(data, args.tasks)
    except ValueError as e:
        ap.error(str(e))
    batch_dir = run_batch(data, tasks, write=args.write, skip_llm=args.skip_llm,
                          tasks_file=args.tasks_file, allow_permanent=args.allow_permanent)
    print(f"\nNothing scored yet. Score with (from agent/):\n  uv run python -m harness.score {batch_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
