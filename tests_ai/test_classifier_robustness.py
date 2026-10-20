"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

The two failures of the first hosted run (model deepseek-v4p1-flash):
- week1_refuse_commission: the classifier got empty content back at
  max_tokens=512 and the run ended with an error. parse_request now retries
  once (more tokens, schema in the prompt) and run.py falls back to the chat
  loop instead of exiting.
- week1_refuse_email: the classifier filed "email the quotation" under
  `other`. The prompt now states what the agent can do and puts actions it
  has no tool for under out_of_scope.
call_llm is scripted; nothing touches the network.
"""
from __future__ import annotations

import json
import sys

import pytest

import run as run_module
from llm import intent
from llm.chat_loop import SYSTEM_PROMPT
from llm.intent import IntentError, classifier_fallback, parse_request, record_no_tool
from transport.llm_client import LLMError

CLASSIFIED = {"asks": [], "item_ref": "", "qty": 0,
              "out_of_scope": [{"request": "commission", "reason": "payroll"}], "other": ""}
EMPTY = {"text": "", "parsed": None, "stop_reason": "max_tokens"}


@pytest.fixture
def llm(monkeypatch):
    """Scripted replies, one per call (an Exception is raised). Records each call."""
    calls = []

    def install(*replies):
        replies = list(replies)

        def fake(messages, **kw):
            calls.append({"messages": messages, **kw})
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(intent, "call_llm", fake)
        return calls
    return install


# --- issue 1: empty / unusable classifier replies ---------------------------------

def test_first_call_has_room_for_a_reasoning_model(llm):
    calls = llm({"parsed": CLASSIFIED})
    parse_request("q")
    assert calls[0]["max_tokens"] == intent.MAX_TOKENS >= 2048
    assert calls[0]["response_schema"] is intent.SCHEMA


def test_empty_reply_is_retried_with_more_room_and_schema_in_prompt(llm):
    calls = llm(EMPTY, {"text": json.dumps(CLASSIFIED)})
    out = parse_request("What commission will each owner earn?")
    assert out["route"] == "graph" and out["out_of_scope"][0]["request"] == "commission"
    assert len(calls) == 2
    retry = calls[1]
    assert retry["max_tokens"] == intent.RETRY_MAX_TOKENS
    assert retry["response_schema"] is None
    assert "JSON Schema" in retry["messages"][0]["content"]
    assert retry["messages"][0]["content"].startswith(intent.SYSTEM_PROMPT)


def test_retry_tolerates_prose_and_a_code_fence(llm):
    llm(EMPTY, {"text": "Here you go:\n```json\n" + json.dumps(CLASSIFIED) + "\n```"})
    assert parse_request("q")["out_of_scope"][0]["reason"] == "payroll"


def test_two_failures_name_each_attempt(llm):
    llm(EMPTY, {"text": "I think this is about commission.", "stop_reason": "end_turn"})
    with pytest.raises(IntentError) as e:
        parse_request("q")
    msg = str(e.value)
    assert msg.startswith("classifier returned no JSON")
    assert "attempt 1 (max_tokens=2048): stop_reason='max_tokens', 0 chars" in msg
    assert "attempt 2 (max_tokens=4096): stop_reason='end_turn'" in msg


def test_model_error_is_not_retried(llm):
    calls = llm(LLMError("model HTTP 401"))
    with pytest.raises(IntentError, match="could not classify"):
        parse_request("q")
    assert len(calls) == 1


def test_non_object_json_is_not_retried(llm):
    calls = llm({"text": json.dumps(["at_risk"])})
    with pytest.raises(IntentError, match="not an object"):
        parse_request("q")
    assert len(calls) == 1


def test_fallback_intent_routes_to_chat_with_nothing_decided():
    fb = classifier_fallback("classifier returned no JSON: ...")
    assert fb["route"] == "chat"
    assert fb["asks"] == [] and fb["out_of_scope"] == [] and fb["other"] is None
    assert fb["classifier_error"].startswith("classifier returned no JSON")
    for key in ("asks", "item_ref", "qty", "out_of_scope", "other"):   # run.py's plan_args
        assert key in fb


def test_run_falls_back_to_chat_when_classifier_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_module, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(run_module, "load_env", lambda: {"TOKEN": "t"})
    monkeypatch.setattr(run_module, "client_from_env", lambda env: object())

    def no_json(q):
        raise IntentError("classifier returned no JSON: attempt 1 ...")
    monkeypatch.setattr(run_module, "parse_request", no_json)
    seen = {}

    def fake_chat(query, session_id, client, dry_run=False, today=None, refused=None):
        seen.update(query=query, refused=refused)
        return ("I can't tell you commissions: that's payroll, not sales.",
                {"refusals": [{"outcome": "refused", "request": "commission", "reason": "payroll"}]},
                [], {"source": "llm", "problems": []})
    monkeypatch.setattr(run_module.chat_loop, "run", fake_chat)
    monkeypatch.setattr(sys, "argv", ["run.py", "What commission will owners earn?",
                                      "--dry-run", "--run-id", "r1", "--today", "2026-09-28"])

    assert run_module.main() == 0
    rec = json.loads((tmp_path / "r1" / "taskrun.json").read_text())
    assert rec["ended"] == "done"
    assert rec["intent"]["route"] == "chat" and "classifier_error" in rec["intent"]
    assert rec["finding"]["refusals"][0]["request"] == "commission"
    step = rec["steps"][0]
    assert step["target"] == "parse_request" and step["status"] == "error"
    assert any("classifier failed, routed to chat" in w for w in rec["warnings"])
    assert seen["query"] == "What commission will owners earn?"
    assert seen["refused"] == [{"request": "commission",
                                "reason": "not a sales agent's job (payroll)"}]
    assert rec["intent"]["out_of_scope"] == seen["refused"]


# --- issue 2: actions with no tool belong in out_of_scope -------------------------

def test_prompt_states_what_the_agent_cannot_do():
    p = intent.SYSTEM_PROMPT
    assert "It cannot mark a deal won or lost, send email or messages, start follow-up" in " ".join(p.split())
    assert "an action the agent has no" in p
    assert "Never put an action from (b) here" in " ".join(p.split())


def test_schema_descriptions_match_the_prompt():
    props = intent.SCHEMA["properties"]
    assert "no tool for" in props["out_of_scope"]["description"]
    assert "looking up one deal or lead" in props["other"]["description"]


def test_email_filed_under_other_is_recorded_as_a_refusal():
    """The hosted failure: the classifier put the email request in `other`."""
    query = "Email the latest quotation to the deal's contact and start a follow-up sequence."
    out = record_no_tool(
        {"route": "chat", "asks": [], "item_ref": None, "qty": None,
         "out_of_scope": [], "other": query},
        query)
    assert out["route"] == "graph"
    assert out["other"] is None
    assert out["out_of_scope"][0]["request"] == "email or follow-up sequence"


def test_lookup_stays_on_the_chat_path():
    out = record_no_tool(
        {"route": "chat", "asks": [], "item_ref": None, "qty": None,
         "out_of_scope": [], "other": "look up deal X"},
        "look up deal X")
    assert out["route"] == "chat" and out["other"] == "look up deal X"
    assert out["out_of_scope"] == []


def test_failed_classification_keeps_chat_but_records_the_refusal():
    query = "What commission will each deal owner earn on the deals closing this month?"
    out = record_no_tool(classifier_fallback("classifier returned no JSON"), query, keep_route=True)
    assert out["route"] == "chat" and out["classifier_error"]
    assert out["out_of_scope"][0]["request"] == "commission"


def test_chat_prompt_names_the_no_tool_actions():
    assert "record a refusal with refuse_request" in SYSTEM_PROMPT
    for phrase in ("Emailing a quotation", "follow-up sequence", "merging records",
                   "raising an invoice", "commission"):
        assert phrase in SYSTEM_PROMPT
