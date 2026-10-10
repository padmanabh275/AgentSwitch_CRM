"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

The hosted run's time limits: runner.run_batch kills an agent run after its
timeout, never gives a task more than the deadline allows, and skips tasks
once the budget is spent; hosted.main passes the limits through and records
SIGTERM as a crash in results.json. Nothing touches the network.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

import pytest

from harness import hosted, runner

TASKS = [{"id": f"t{i}", "description": f"Task {i}"} for i in range(4)]
DATA = {"format": runner.TASKS_FORMAT, "today": "2026-09-28", "tasks": TASKS}


@pytest.fixture
def offline_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "llm_gateway_up", lambda: True)
    monkeypatch.setattr(runner, "client_from_env", lambda env: object())
    monkeypatch.setattr(runner, "load_env", lambda: {})
    monkeypatch.setattr(runner.verifiers, "observations_for", lambda task: [])
    monkeypatch.setattr(runner.verifiers, "observe_all", lambda client, keys: {})
    return tmp_path


def test_real_hanging_agent_is_killed_at_the_timeout(monkeypatch, tmp_path):
    (tmp_path / "run.py").write_text("import time; time.sleep(30)\n")
    monkeypatch.setattr(runner, "AGENT_DIR", tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    t0 = time.monotonic()
    result = runner.run_agent({"id": "x", "argv": []}, "r", "2026-09-28", False, run_dir,
                              timeout_s=0.5)
    assert time.monotonic() - t0 < 10
    assert result["exit_code"] == "timeout"
    assert "timed out after" in (run_dir / "stdout.txt").read_text()


def test_without_deadline_every_task_gets_the_task_timeout(offline_runner, monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "run_agent", lambda task, run_id, today, write, run_dir, t:
                        seen.append(t) or {"exit_code": 0, "seconds": 1})
    runner.run_batch(DATA, TASKS, agent_timeout_s=180)
    assert seen == [180] * 4


def test_deadline_caps_the_timeout_and_skips_when_spent(offline_runner, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["now"])
    seen = []

    def fake_run_agent(task, run_id, today, write, run_dir, t):
        seen.append(round(t))
        clock["now"] += 100            # each task uses 100s of the budget
        return {"exit_code": 0, "seconds": 100}

    monkeypatch.setattr(runner, "run_agent", fake_run_agent)
    entries = []
    # 360s budget minus the 60s reserve leaves 300, 200, 100, 0 at each task's start:
    # capped at 180, 180, then 100, then under MIN_AGENT_S -> skipped.
    runner.run_batch(DATA, TASKS, agent_timeout_s=180, deadline=1000.0 + 360,
                     on_task_done=entries.append)
    assert seen == [180, 180, 100]
    assert [e["status"] for e in entries] == ["ran", "ran", "ran", "skipped"]
    assert entries[-1]["reason"] == "time budget exhausted before this task started"


def test_short_task_timeout_does_not_look_like_a_spent_budget(offline_runner, monkeypatch):
    # Found live: --task-timeout 5 with plenty of budget skipped every task.
    seen = []
    monkeypatch.setattr(runner, "run_agent", lambda task, run_id, today, write, run_dir, t:
                        seen.append(t) or {"exit_code": 0, "seconds": 1})
    entries = []
    runner.run_batch(DATA, TASKS, agent_timeout_s=5, deadline=time.monotonic() + 600,
                     on_task_done=entries.append)
    assert seen == [5] * 4
    assert [e["status"] for e in entries] == ["ran"] * 4


def test_budget_flags_reach_run_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "load_tasks", lambda: DATA)
    seen = {}

    def fake_run_batch(data, tasks, write=False, on_task_done=None, agent_timeout_s=None,
                       deadline=None, **kw):
        seen.update(timeout=agent_timeout_s, left=deadline - time.monotonic())

    monkeypatch.setattr(runner, "run_batch", fake_run_batch)
    hosted.main(["--out", str(tmp_path / "r.json"), "--task-timeout", "90",
                 "--budget-minutes", "2"])
    assert seen["timeout"] == 90
    assert 110 < seen["left"] <= 120


def test_defaults_fit_a_full_batch_with_margin():
    assert hosted.TASK_TIMEOUT_S >= 3 * 50          # 3x the slowest task on record
    assert hosted.BUDGET_MINUTES <= 30 - 3          # under the platform's max with margin


@pytest.fixture
def restore_sigterm():
    old = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, old)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_is_recorded_and_finished_tasks_kept(monkeypatch, tmp_path, restore_sigterm):
    monkeypatch.setenv("AGENTSWITCH_INSTANCE", "suryodaya")
    monkeypatch.setattr(runner, "load_tasks", lambda: DATA)
    out = tmp_path / "results.json"

    def fake_run_batch(data, tasks, on_task_done=None, **kw):
        on_task_done({"task_id": "t0", "status": "skipped", "reason": "x"})
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)                  # the handler raises before this returns

    monkeypatch.setattr(runner, "run_batch", fake_run_batch)
    with pytest.raises(SystemExit):
        hosted.main(["--out", str(out)])
    doc = json.loads(out.read_text())
    assert [t["id"] for t in doc["tasks"]][-1] == "suryodaya:harness"
    assert "SIGTERM" in doc["tasks"][-1]["evidence"]
    assert doc["tasks"][0]["evidence"] == "skipped: x"
    assert doc["tasks"][1]["evidence"] == hosted.NOT_RUN
