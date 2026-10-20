"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Harness rules from the instructor's list:
- the platform is AGENTSWITCH_BASE_URL (an internal sandbox): a hosted token
  without it is an error, never a fall-back to the public instance;
- a spent token budget is HTTP 429 budget_exhausted, not 401: it gets its own
  code, is never retried, and stops the batch with that reason.
Nothing touches the network.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from harness import hosted, runner
from transport import llm_client, mcp_client
from transport.llm_client import LLMBudgetExhausted, call_llm
from transport.mcp_client import (BUDGET_EXHAUSTED, TRANSIENT, MCPClient, MCPToolError,
                                  instance_from_env)

BUDGET_BODY = json.dumps({"error": {"code": "budget_exhausted",
                                    "message": "token budget spent"}}).encode()


def http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


# --- base URL -------------------------------------------------------------------

def test_instance_is_unknown_for_a_token_without_base_url():
    assert instance_from_env({"AGENTSWITCH_TOKEN": "t"}) is None
    assert instance_from_env({"AGENTSWITCH_TOKEN": "t", "AGENTSWITCH_INSTANCE": "keystone"}) == "keystone"


# --- platform 429 ---------------------------------------------------------------

@pytest.fixture
def mcp(monkeypatch):
    replies = []

    def urlopen(req, timeout=None):
        raise replies.pop(0)
    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", urlopen)
    return MCPClient("", "", base_url="http://sandbox.internal", token="t"), replies


def test_platform_budget_429_has_its_own_code(mcp):
    client, replies = mcp
    replies.append(http_error(429, BUDGET_BODY))
    with pytest.raises(MCPToolError) as e:
        client.call("Deal.list", {})
    assert e.value.code == BUDGET_EXHAUSTED
    assert "budget_exhausted" in str(e.value)


def test_platform_rate_limit_429_is_still_transient(mcp):
    client, replies = mcp
    replies.append(http_error(429, b'{"error": "slow down"}'))
    with pytest.raises(MCPToolError) as e:
        client.call("Deal.list", {})
    assert e.value.code == TRANSIENT


def test_budget_is_not_mistaken_for_auth(mcp):
    # A token-only client has no password, so an `auth` error would not retry
    # either; the point is the code callers branch on.
    client, replies = mcp
    replies.append(http_error(429, BUDGET_BODY))
    with pytest.raises(MCPToolError) as e:
        client.call("Deal.list", {})
    assert e.value.code != mcp_client.AUTH


# --- model 429 ------------------------------------------------------------------

@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.internal/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    state = {"replies": [], "requests": 0}

    def urlopen(req, timeout=None):
        state["requests"] += 1
        raise state["replies"].pop(0)
    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)
    return state


def test_model_budget_429_is_raised_at_once_without_retry(model):
    model["replies"] += [http_error(429, BUDGET_BODY)] * (llm_client.TRANSIENT_RETRIES + 1)
    with pytest.raises(LLMBudgetExhausted, match="budget_exhausted"):
        call_llm([{"role": "user", "content": "q"}])
    assert model["requests"] == 1


def test_model_budget_error_is_still_an_llm_error(model):
    # Callers that catch LLMError (intent, narrate) keep working.
    assert issubclass(LLMBudgetExhausted, llm_client.LLMError)


# --- the batch stops ------------------------------------------------------------

TASKS = [{"id": f"t{i}", "description": f"Task {i}"} for i in range(3)]
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


def test_batch_skips_the_rest_once_a_run_hits_the_budget(offline_runner, monkeypatch):
    ran = []

    def fake_run_agent(task, run_id, today, write, run_dir, t):
        ran.append(task["id"])
        if task["id"] == "t0":
            (run_dir / "calls.jsonl").write_text(json.dumps(
                {"tool": "Deal.list", "ok": False,
                 "error": "Deal.list: HTTP 429 budget_exhausted"}) + "\n")
        return {"exit_code": 0, "seconds": 1}

    monkeypatch.setattr(runner, "run_agent", fake_run_agent)
    entries = []
    runner.run_batch(DATA, TASKS, on_task_done=entries.append)
    assert ran == ["t0"]
    assert [e["status"] for e in entries] == ["ran", "skipped", "skipped"]
    assert entries[0]["budget_exhausted"] is True
    assert all(e["reason"] == runner.BUDGET_REASON for e in entries[1:])


def test_harness_side_budget_error_also_stops_the_batch(offline_runner, monkeypatch):
    def fixture_read_fails(*a, **kw):
        raise MCPToolError("Party.list: HTTP 429 budget_exhausted", code=BUDGET_EXHAUSTED)
    monkeypatch.setattr(runner, "run_task", fixture_read_fails)
    entries = []
    runner.run_batch(DATA, TASKS, on_task_done=entries.append)
    assert [e["status"] for e in entries] == ["error", "skipped", "skipped"]


def test_an_ordinary_failure_does_not_stop_the_batch(offline_runner, monkeypatch):
    monkeypatch.setattr(runner, "run_agent", lambda task, run_id, today, write, run_dir, t:
                        {"exit_code": 1, "seconds": 1})
    entries = []
    runner.run_batch(DATA, TASKS, on_task_done=entries.append)
    assert [e["status"] for e in entries] == ["ran", "ran", "ran"]


def test_results_summary_says_the_budget_ran_out():
    entries = {"t0": {"status": "ran", "budget_exhausted": True},
               "t1": {"status": "skipped", "reason": runner.BUDGET_REASON}}
    out = hosted.build_results(TASKS[:2], entries, {}, "suryodaya", "write")
    assert "token budget exhausted (429)" in out["summary"]
    assert "429 budget_exhausted" in out["tasks"][1]["evidence"]
