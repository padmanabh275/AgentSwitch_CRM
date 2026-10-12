"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

transport.llm_client's OpenAI-compatible backend: callers keep glc_v5's shapes
(tool_calls as {id,name,arguments}, stop_reason, parsed) while the wire format
is OpenAI chat/completions. urlopen is stubbed; nothing touches the network.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from transport import llm_client
from transport.llm_client import LLMError, call_llm

SPEC = [{"name": "get_deal", "description": "One deal by id.",
         "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}]


class FakeResponse:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw


def http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


@pytest.fixture
def server(monkeypatch):
    """Scripted replies: each item is a dict (JSON body) or an exception. Records requests."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.local/v1/")
    monkeypatch.setenv("OPENAI_API_KEY", "k-123")
    monkeypatch.setenv("OPENAI_MODEL", "platform-model")
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    state = {"replies": [], "requests": []}

    def urlopen(req, timeout=None):
        state["requests"].append({"url": req.full_url, "headers": dict(req.header_items()),
                                  "body": json.loads(req.data)})
        item = state["replies"].pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)
    return state


def reply(content=None, tool_calls=None, finish="stop"):
    return {"model": "platform-model", "choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content, "tool_calls": tool_calls}}]}


# --- backend selection ----------------------------------------------------------

def test_no_openai_vars_means_glc():
    assert llm_client.openai_config({}) is None


def test_api_key_alone_selects_openai_with_defaults():
    cfg = llm_client.openai_config({"OPENAI_API_KEY": "k"})
    assert cfg == {"base_url": llm_client.DEFAULT_OPENAI_BASE_URL, "api_key": "k",
                   "model": llm_client.DEFAULT_OPENAI_MODEL}


def test_backend_url_follows_env(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert llm_client.backend_url() == llm_client.GLC_URL
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.local/v1/")
    assert llm_client.backend_url() == "http://model.local/v1"


# --- request shape --------------------------------------------------------------

def test_request_goes_to_chat_completions_with_bearer_model_and_tools(server):
    server["replies"].append(reply("hi"))
    call_llm([{"role": "user", "content": "q"}], tools=SPEC)
    req = server["requests"][0]
    assert req["url"] == "http://model.local/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer k-123"
    body = req["body"]
    assert body["model"] == "platform-model"
    assert body["tool_choice"] == "auto"
    assert body["tools"] == [{"type": "function", "function": {
        "name": "get_deal", "description": "One deal by id.",
        "parameters": SPEC[0]["input_schema"]}}]
    assert "provider" not in body and "agent" not in body


def test_tool_round_trip_messages_are_translated(server):
    server["replies"].append(reply("done"))
    call_llm([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "name": "get_deal", "arguments": {"id": "D-1"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "get_deal", "content": '{"outcome":"ok"}'},
    ], tools=SPEC)
    msgs = server["requests"][0]["body"]["messages"]
    assert msgs[2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_deal", "arguments": '{"id": "D-1"}'}}]}
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"outcome":"ok"}'}


# --- reply shape ----------------------------------------------------------------

def test_tool_calls_come_back_in_glc_shape(server):
    server["replies"].append(reply(None, finish="tool_calls", tool_calls=[
        {"id": "c9", "type": "function",
         "function": {"name": "get_deal", "arguments": '{"id": "D-7"}'}}]))
    resp = call_llm([{"role": "user", "content": "q"}], tools=SPEC)
    assert resp["tool_calls"] == [{"id": "c9", "name": "get_deal", "arguments": {"id": "D-7"}}]
    assert resp["text"] == ""
    assert resp["stop_reason"] == "tool_use"


def test_bad_tool_arguments_become_empty_dict_and_missing_id_is_filled(server):
    server["replies"].append(reply(None, finish="tool_calls", tool_calls=[
        {"function": {"name": "get_deal", "arguments": "not json"}}]))
    resp = call_llm([{"role": "user", "content": "q"}], tools=SPEC)
    assert resp["tool_calls"] == [{"id": "call_0", "name": "get_deal", "arguments": {}}]


def test_length_finish_maps_to_max_tokens(server):
    server["replies"].append(reply("cut", finish="length"))
    assert call_llm([{"role": "user", "content": "q"}])["stop_reason"] == "max_tokens"


def test_reply_without_choices_raises(server):
    server["replies"].append({"error": "nope"})
    with pytest.raises(LLMError, match="no choices"):
        call_llm([{"role": "user", "content": "q"}])


# --- structured output ----------------------------------------------------------

SCHEMA = {"type": "object", "properties": {"asks": {"type": "array"}}}


def test_schema_is_sent_as_json_schema_and_parsed(server):
    server["replies"].append(reply('```json\n{"asks": ["at_risk"]}\n```'))
    resp = call_llm([{"role": "user", "content": "q"}], response_schema=SCHEMA)
    rf = server["requests"][0]["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == SCHEMA
    assert resp["parsed"] == {"asks": ["at_risk"]}


def test_400_on_schema_retries_once_with_schema_in_prompt(server):
    server["replies"] += [http_error(400, b"response_format unsupported"),
                          reply('Sure: {"asks": []}')]
    resp = call_llm([{"role": "system", "content": "classify"},
                     {"role": "user", "content": "q"}], response_schema=SCHEMA)
    retry = server["requests"][1]["body"]
    assert "response_format" not in retry and "temperature" not in retry
    assert retry["messages"][0]["content"].startswith("classify")
    assert "JSON Schema" in retry["messages"][0]["content"]
    assert resp["parsed"] == {"asks": []}


def test_second_400_raises_llm_error(server):
    server["replies"] += [http_error(400), http_error(400)]
    with pytest.raises(LLMError, match="400"):
        call_llm([{"role": "user", "content": "q"}])


# --- transient failures ---------------------------------------------------------

def test_503_then_success_is_retried(server):
    server["replies"] += [http_error(503), reply("ok")]
    assert call_llm([{"role": "user", "content": "q"}])["text"] == "ok"


def test_persistent_429_gives_up_as_llm_error(server):
    server["replies"] += [http_error(429)] * (llm_client.TRANSIENT_RETRIES + 1)
    with pytest.raises(LLMError, match="429"):
        call_llm([{"role": "user", "content": "q"}])


def test_unreachable_gives_up_as_llm_error(server):
    server["replies"] += [urllib.error.URLError("refused")] * (llm_client.TRANSIENT_RETRIES + 1)
    with pytest.raises(LLMError, match="unreachable"):
        call_llm([{"role": "user", "content": "q"}])


def test_401_is_not_retried(server):
    server["replies"].append(http_error(401))
    with pytest.raises(LLMError, match="401"):
        call_llm([{"role": "user", "content": "q"}])
    assert len(server["requests"]) == 1
