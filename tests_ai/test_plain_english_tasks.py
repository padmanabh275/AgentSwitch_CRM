"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Every harness task is a request a person would type. A task that hands the
agent flags (--asks, --item, --deal, --order-quote ...) skips the classifier,
so a misread request would pass every check; validate_task now refuses one.
Putting a quote on a deal was only reachable through --deal, so the
classifier gained quote_deal_ref, and the graph finds that deal by id or
exact title.

Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.quotations import read_open_deal_ref
from graph import plans
from harness.verifiers import validate_task
from llm.intent import classifier_fallback, validate

TASKS = json.loads((Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]
DEAL_ID = "4f00455c-fba3-4165-b080-1a261033700d"
OPEN_DEAL = {"id": DEAL_ID, "title": "Acme Q4", "stage": "proposal", "party_id": "p1",
             "company_id": "c1", "updated_at": "t0"}


def raw(**fields):
    return {"asks": [], "item_ref": "", "qty": 0, "quote_deal_ref": "", "out_of_scope": [], "other": "",
            **fields}


# --- the task file ----------------------------------------------------------

def every_request(task):
    yield task["id"], task.get("argv")
    for instance, override in (task.get("by_instance") or {}).items():
        yield f"{task['id']}@{instance}", override.get("argv")
    for step in task.get("steps") or []:
        yield f"{task['id']}.{step['name']}", step.get("argv")


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_every_task_asks_in_plain_english(task):
    validate_task(task)
    assert task["requires_llm"]
    for where, argv in every_request(task):
        if argv is not None:
            assert len(argv) == 1 and not argv[0].startswith("-"), where


@pytest.mark.parametrize("argv", [
    ["--asks", "quote", "--item", "ST-VICE-150", "--qty", "500"],
    ["--chat", "Look up deal x"],
    ["Quote 500 units of ST-VICE-150.", "--deal", "d1"],
    [],
    ["  "],
])
def test_flags_are_refused(argv):
    with pytest.raises(ValueError, match="plain-English"):
        validate_task({"id": "t", "argv": argv, "requires_llm": True})


def test_a_request_without_the_llm_is_refused():
    with pytest.raises(ValueError, match="requires_llm"):
        validate_task({"id": "t", "argv": ["Which open deals are at risk?"], "requires_llm": False})


def test_an_instance_override_inherits_requires_llm():
    validate_task({"id": "t", "argv": ["Quote 500 units of A."], "requires_llm": True,
                   "by_instance": {"keystone": {"argv": ["Quote 500 units of B."]}}})
    with pytest.raises(ValueError, match="plain-English"):
        validate_task({"id": "t", "argv": ["Quote 500 units of A."], "requires_llm": True,
                       "by_instance": {"keystone": {"argv": ["--item", "B"]}}})


# --- the classifier ---------------------------------------------------------

def test_a_quote_on_a_deal_keeps_the_deal():
    out = validate(raw(asks=["quote"], item_ref="ST-VICE-150", qty=500, quote_deal_ref=" Acme Q4 "))
    assert out["quote_deal_ref"] == "Acme Q4" and out["route"] == "graph"


@pytest.mark.parametrize("fields", [
    {"asks": ["at_risk"], "quote_deal_ref": "Acme Q4"},   # no quote: a deal would write nothing
    {"asks": ["quote"], "quote_deal_ref": "   "},
    {"asks": ["quote"]},
])
def test_no_quote_deal_without_a_quote_and_a_deal(fields):
    assert validate(raw(**fields))["quote_deal_ref"] is None


def test_an_item_and_a_quantity_without_the_ask_is_a_quote():
    # gemma3:12b, live 2026-10-23: everything right but asks
    out = validate(raw(item_ref="ASP-H-011", qty=500, quote_deal_ref="Acme Q4"))
    assert out["asks"] == ["quote"] and out["route"] == "graph" and out["quote_deal_ref"] == "Acme Q4"
    assert "quote_inferred" in out


def test_an_inferred_quote_keeps_the_other_asks_in_order():
    out = validate(raw(asks=["at_risk", "closing_this_month"], item_ref="X", qty=5))
    assert out["asks"] == ["closing_this_month", "at_risk", "quote"]


@pytest.mark.parametrize("fields", [
    {"item_ref": "ASP-H-011"},              # no quantity: not enough to call it a quote
    {"qty": 500},                           # no item
    {"asks": ["quote"], "item_ref": "X", "qty": 5},   # the model said so itself
])
def test_no_quote_is_inferred_without_both_or_when_asked(fields):
    out = validate(raw(**fields))
    assert "quote_inferred" not in out
    assert ("quote" in out["asks"]) == ("quote" in fields.get("asks", []))


def test_the_fallback_names_no_deal():
    assert classifier_fallback("x")["quote_deal_ref"] is None


# --- the plan ---------------------------------------------------------------

def get_deal_args(**kw):
    specs = plans.pipeline_review(["quote"], item_ref="ST-VICE-150", qty=500, **kw)
    finding = next(s for s in specs if s.id == "build_finding")
    deal = next((s for s in specs if s.id == "get_deal"), None)
    return (deal.args if deal else None), finding.args["quotation_requested"]


def test_a_named_deal_is_looked_up_by_reference():
    assert get_deal_args(quote_deal_ref="Acme Q4") == ({"deal_ref": "Acme Q4"}, True)


def test_a_deal_id_still_reads_the_deal_directly():
    assert get_deal_args(deal_id="d1", quote_deal_ref="Acme Q4") == ({"deal_id": "d1"}, True)


def test_no_deal_means_no_quotation():
    assert get_deal_args() == (None, False)


# --- finding the deal -------------------------------------------------------

class Deals:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "Deal.get":
            return next(r for r in self.rows if r["id"] == args["id"])
        if tool == "Deal.list":   # the server may ignore the filter; find_deal checks the titles itself
            return {"data": self.rows}
        raise AssertionError(f"unexpected tool call {tool}")


def test_a_deal_title_resolves_to_its_id():
    client = Deals([OPEN_DEAL, {**OPEN_DEAL, "id": "other", "title": "Acme Q3"}])
    out = read_open_deal_ref(client, "acme q4")
    assert out["outcome"] == "answered" and out["deal"]["id"] == DEAL_ID
    assert client.calls[-1] == ("Deal.get", {"id": DEAL_ID})


def test_a_deal_id_is_read_without_a_title_search():
    client = Deals([OPEN_DEAL])
    assert read_open_deal_ref(client, DEAL_ID)["outcome"] == "answered"
    assert [t for t, _ in client.calls] == ["Deal.get"]


@pytest.mark.parametrize("rows, why", [
    ([], "no deal is titled"),
    ([OPEN_DEAL, {**OPEN_DEAL, "id": "twin"}], "give the deal id"),
])
def test_a_title_that_names_no_single_deal_is_refused(rows, why):
    out = read_open_deal_ref(Deals(rows), "Acme Q4")
    assert out["outcome"] == "refused" and why in out["reason"] and out["deal_id"] is None


def test_a_closed_deal_found_by_title_is_still_refused():
    out = read_open_deal_ref(Deals([{**OPEN_DEAL, "stage": "closed_won"}]), "Acme Q4")
    assert out["outcome"] == "refused" and "not open" in out["reason"]
