"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

harness.hosted: results.json must always match the platform's format
({"tasks": [{id,title,passed,score,evidence}], "summary"}), exist before the
first task runs, be rewritten after each task, and survive a crash.
runner.run_batch and score.score_run are stubbed; nothing touches the network.
"""
from __future__ import annotations

import json

import pytest

from harness import hosted, runner, score

TASKS = [{"id": "a", "description": "Task A"}, {"id": "b", "description": "Task B"},
         {"id": "c", "description": "Task C"}]
DATA = {"format": runner.TASKS_FORMAT, "today": "2026-09-28", "tasks": TASKS}


def check(status, verifier="v", name="n", detail="d"):
    return {"verifier": verifier, "name": name, "status": status, "detail": detail}


PASS_SCORE = {"status": "pass", "checks": [check("pass"), check("pass"), check("skip")]}
FAIL_SCORE = {"status": "fail", "checks": [check("pass"), check("fail", "deal_set", "ids",
                                                                 "missing D-1")]}


def assert_platform_format(doc):
    assert set(doc) == {"tasks", "summary"} and isinstance(doc["summary"], str)
    assert 1 <= len(doc["tasks"]) <= 200
    ids = [t["id"] for t in doc["tasks"]]
    assert len(ids) == len(set(ids))
    for t in doc["tasks"]:
        assert set(t) == {"id", "title", "passed", "score", "evidence"}
        assert isinstance(t["passed"], bool)
        assert 0.0 <= t["score"] <= 1.0
        assert isinstance(t["evidence"], str) and len(t["evidence"]) <= hosted.EVIDENCE_CHARS


# --- mapping one task ---------------------------------------------------------------

def test_pass_ignores_skipped_checks_in_score():
    r = hosted.task_result(TASKS[0], {"seconds": 3.2}, PASS_SCORE, "suryodaya")
    assert r == {"id": "suryodaya:a", "title": "Task A", "passed": True, "score": 1.0,
                 "evidence": "pass: 2/2 checks pass (3.2s)"}


def test_fail_names_the_failing_check():
    r = hosted.task_result(TASKS[0], {"seconds": 1}, FAIL_SCORE, "suryodaya")
    assert r["passed"] is False and r["score"] == 0.5
    assert "deal_set/ids fail: missing D-1" in r["evidence"]


def test_drift_is_not_a_pass():
    drift = {"status": "drift", "checks": [check("pass"), check("drift")]}
    assert hosted.task_result(TASKS[0], {}, drift, "s")["passed"] is False


@pytest.mark.parametrize("entry,expected", [
    (None, hosted.NOT_RUN),
    ({"status": "skipped", "reason": "LLM backend unreachable"}, "skipped: LLM backend unreachable"),
    ({"status": "error", "reason": "URLError: refused"}, "error: URLError: refused"),
    ({"status": "ran", "exit_code": 1, "seconds": 2}, "no score: no taskrun.json was written"),
])
def test_unscored_tasks_fail_with_reason(entry, expected):
    r = hosted.task_result(TASKS[0], entry, None, "s")
    assert r["passed"] is False and r["score"] == 0.0
    assert r["evidence"].startswith(expected)


def test_long_evidence_and_title_are_truncated():
    long = {"status": "fail", "checks": [check("fail", detail="x" * 2000)]}
    r = hosted.task_result({"id": "a", "description": "t" * 500}, {}, long, "s")
    assert len(r["evidence"]) == hosted.EVIDENCE_CHARS
    assert len(r["title"]) == hosted.TITLE_CHARS


def test_build_results_with_crash_adds_harness_task():
    doc = hosted.build_results(TASKS, {}, {}, "keystone", "dry run", crash="RuntimeError: boom")
    assert_platform_format(doc)
    assert doc["tasks"][-1]["id"] == "keystone:harness"
    assert "harness crashed" in doc["summary"]


# --- the whole run ------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSWITCH_INSTANCE", "suryodaya")
    monkeypatch.setattr(runner, "load_tasks", lambda: DATA)
    out = tmp_path / "results.json"
    snapshots = []

    def fake_score_run(run_dir):
        return PASS_SCORE if run_dir.name == "a" else FAIL_SCORE

    monkeypatch.setattr(score, "score_run", fake_score_run)

    def entry_for(tmp, task_id):
        run_dir = tmp / task_id
        run_dir.mkdir(exist_ok=True)
        (run_dir / "taskrun.json").write_text("{}")
        return {"task_id": task_id, "status": "ran", "run_dir": str(run_dir),
                "exit_code": 0, "seconds": 1.0}

    return {"out": out, "snapshots": snapshots, "entry_for": lambda i: entry_for(tmp_path, i)}


def test_file_exists_before_first_task_and_after_each(env, monkeypatch):
    def fake_run_batch(data, tasks, write=False, on_task_done=None, **kw):
        env["snapshots"].append(json.loads(env["out"].read_text()))
        for t in tasks:
            on_task_done(env["entry_for"](t["id"]))
            env["snapshots"].append(json.loads(env["out"].read_text()))

    monkeypatch.setattr(runner, "run_batch", fake_run_batch)
    assert hosted.main(["--out", str(env["out"])]) == 0

    first = env["snapshots"][0]
    assert_platform_format(first)
    assert [t["evidence"] for t in first["tasks"]] == [hosted.NOT_RUN] * 3
    after_one = env["snapshots"][1]
    assert [t["passed"] for t in after_one["tasks"]] == [True, False, False]
    assert after_one["tasks"][1]["evidence"] == hosted.NOT_RUN

    final = json.loads(env["out"].read_text())
    assert_platform_format(final)
    assert [t["id"] for t in final["tasks"]] == ["suryodaya:a", "suryodaya:b", "suryodaya:c"]
    assert [t["passed"] for t in final["tasks"]] == [True, False, False]
    assert final["summary"].startswith("suryodaya: 1/3 tasks passed · dry run")


def test_crash_mid_batch_keeps_finished_tasks(env, monkeypatch):
    def fake_run_batch(data, tasks, write=False, on_task_done=None, **kw):
        on_task_done(env["entry_for"]("a"))
        raise RuntimeError("MCP went away")

    monkeypatch.setattr(runner, "run_batch", fake_run_batch)
    assert hosted.main(["--out", str(env["out"])]) == 0
    doc = json.loads(env["out"].read_text())
    assert_platform_format(doc)
    assert [t["passed"] for t in doc["tasks"]] == [True, False, False, False]
    assert doc["tasks"][1]["evidence"] == hosted.NOT_RUN
    assert "MCP went away" in doc["tasks"][-1]["evidence"]


def test_crash_before_task_list_still_writes_one_task(env, monkeypatch):
    def broken():
        raise SystemExit("tasks.json: unsupported format")

    monkeypatch.setattr(runner, "load_tasks", broken)
    with pytest.raises(SystemExit):
        hosted.main(["--out", str(env["out"])])
    doc = json.loads(env["out"].read_text())
    assert_platform_format(doc)
    assert [t["id"] for t in doc["tasks"]] == ["suryodaya:harness"]


def test_scorer_exception_fails_only_that_task(env, monkeypatch):
    def boom(run_dir):
        raise KeyError("checks")

    monkeypatch.setattr(score, "score_run", boom)
    monkeypatch.setattr(runner, "run_batch", lambda data, tasks, on_task_done=None, **kw:
                        [on_task_done(env["entry_for"](t["id"])) for t in tasks])
    hosted.main(["--out", str(env["out"])])
    doc = json.loads(env["out"].read_text())
    assert_platform_format(doc)
    assert all("scoring failed: KeyError" in t["evidence"] for t in doc["tasks"])


def test_write_flag_reaches_run_batch_and_summary(env, monkeypatch):
    seen = {}

    def fake_run_batch(data, tasks, write=False, on_task_done=None, **kw):
        seen["write"] = write

    monkeypatch.setattr(runner, "run_batch", fake_run_batch)
    hosted.main(["--out", str(env["out"]), "--write", "--tasks", "b"])
    doc = json.loads(env["out"].read_text())
    assert seen["write"] is True
    assert [t["id"] for t in doc["tasks"]] == ["suryodaya:b"]
    assert "· write ·" in doc["summary"]
