"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Refusals on the chat path: a refuse_request tool call, and the classifier's
out_of_scope parts when a request is routed to chat, both land in the chat
finding's `refusals` in the graph's shape, without touching the platform.
call_llm is scripted; the MCP client refuses every call.
"""
from __future__ import annotations

import pytest

from llm import chat_loop, tools


class NoPlatform:
    def call(self, name, arguments=None):
        raise AssertionError(f"refusing must not call the platform ({name})")


def scripted(monkeypatch, replies):
    seen = []

    def fake(messages, tools=None, **kw):
        seen.append([dict(m) for m in messages])
        return replies.pop(0)

    monkeypatch.setattr(chat_loop, "call_llm", fake)
    return seen


def tool_call(name, **arguments):
    return {"text": "", "tool_calls": [{"id": "c1", "name": name, "arguments": arguments}],
            "stop_reason": "tool_use"}


def final(text):
    return {"text": text, "tool_calls": [], "stop_reason": "end_turn"}


def test_refuse_request_is_on_the_menu_and_dispatchable():
    assert "refuse_request" in {t["name"] for t in tools.TOOL_SPECS}
    dispatch = tools.build_dispatch(NoPlatform(), None, dry_run=True)
    assert dispatch["refuse_request"]({"request": " email ", "reason": "no tool "}) == \
        {"outcome": "refused", "request": "email", "reason": "no tool"}


def test_refuse_request_call_is_recorded(monkeypatch):
    scripted(monkeypatch, [
        tool_call("refuse_request", request="email the quotation",
                  reason="no email tool on this platform"),
        final("I can't email quotations: there's no tool for it."),
    ])
    answer, collected, trace = chat_loop.run("Email the latest quotation", None, NoPlatform(),
                                             dry_run=True)
    assert collected["refusals"] == [{"outcome": "refused", "request": "email the quotation",
                                      "reason": "no email tool on this platform"}]
    assert trace[0]["tool"] == "refuse_request" and trace[0]["outcome"] == "refused"
    assert "can't" in answer


def test_no_refusal_means_empty_list(monkeypatch):
    scripted(monkeypatch, [final("Here you go.")])
    _, collected, _ = chat_loop.run("hi", None, NoPlatform(), dry_run=True)
    assert collected["refusals"] == []


def test_classifier_refusals_are_recorded_and_told_to_the_model(monkeypatch):
    seen = scripted(monkeypatch, [final("Commission is outside my job; ...")])
    _, collected, _ = chat_loop.run(
        "What commission do owners earn, and follow up on deal X?", None, NoPlatform(),
        dry_run=True, refused=[{"request": "commission", "reason": "payroll, not sales"}])
    assert collected["refusals"] == [{"outcome": "refused", "request": "commission",
                                      "reason": "payroll, not sales"}]
    system = seen[0][0]["content"]
    assert "Already refused" in system and "- commission: payroll, not sales" in system


def test_classifier_and_model_refusals_accumulate(monkeypatch):
    scripted(monkeypatch, [tool_call("refuse_request", request="merge", reason="no merge tool"),
                           final("Refused both.")])
    _, collected, _ = chat_loop.run("q", None, NoPlatform(), dry_run=True,
                                    refused=[{"request": "commission", "reason": "payroll"}])
    assert [r["request"] for r in collected["refusals"]] == ["commission", "merge"]


def test_refusals_survive_the_iteration_cap(monkeypatch):
    replies = [tool_call("refuse_request", request="x", reason="y")] * chat_loop.MAX_TOOL_ITERATIONS
    scripted(monkeypatch, list(replies))
    answer, collected, _ = chat_loop.run("q", None, NoPlatform(), dry_run=True)
    assert answer.startswith("(stopped after max tool iterations")
    assert len(collected["refusals"]) == chat_loop.MAX_TOOL_ITERATIONS


def test_chat_finding_now_satisfies_refusals_min():
    from harness.verifiers import check_expectations

    class Files:
        task = {"expect": {"refusals_min": 1}}
        finding = {"refusals": [tools.refusal("email", "no tool")]}
        taskrun = {"intent": {"route": "chat"}}
        chat_trace = []

    [check] = check_expectations(Files(), {})
    assert check.status == "pass", check.detail
