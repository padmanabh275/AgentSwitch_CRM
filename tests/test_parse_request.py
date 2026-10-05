"""llm/intent.parse_request: the classifier call around validate(). call_llm is stubbed."""
from __future__ import annotations

import json

import pytest

from llm import intent
from llm.intent import IntentError, parse_request
from transport.llm_client import LLMError

CLASSIFIED = {"asks": ["quote", "at_risk"], "item_ref": "bench vice", "qty": 500,
              "out_of_scope": [], "other": ""}


@pytest.fixture
def llm(monkeypatch):
    seen = {}

    def install(reply):
        def fake(messages, **kw):
            seen.update(messages=messages, **kw)
            if isinstance(reply, Exception):
                raise reply
            return reply
        monkeypatch.setattr(intent, "call_llm", fake)
        return seen
    return install


def test_parsed_object_is_validated(llm):
    llm({"parsed": CLASSIFIED})
    out = parse_request("what's at risk, and quote 500 bench vices")
    assert out["route"] == "graph"
    assert out["asks"] == ["at_risk", "quote"]
    assert out["item_ref"] == "bench vice" and out["qty"] == 500


def test_falls_back_to_json_in_the_text(llm):
    llm({"parsed": None, "text": json.dumps(CLASSIFIED)})
    assert parse_request("q")["qty"] == 500


def test_asks_for_the_schema_and_sends_the_question(llm):
    seen = llm({"parsed": CLASSIFIED})
    parse_request("quote 500 vices")
    assert seen["response_schema"] is intent.SCHEMA
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][1] == {"role": "user", "content": "quote 500 vices"}


@pytest.mark.parametrize("reply", [{"text": "Sure! Here's the answer."}, {"text": None}, {}])
def test_no_json_raises(llm, reply):
    llm(reply)
    with pytest.raises(IntentError, match="no JSON"):
        parse_request("q")


def test_gateway_failure_raises_intent_error(llm):
    llm(LLMError("glc_v5 unreachable"))
    with pytest.raises(IntentError, match="could not classify"):
        parse_request("q")


def test_non_object_json_raises(llm):
    llm({"text": json.dumps(["at_risk"])})
    with pytest.raises(IntentError, match="not an object"):
        parse_request("q")
