"""graph/plans.py, graph/planner.py and Engine: building and running the graph offline."""
from __future__ import annotations

import datetime as dt

import pytest

from graph import engine, plans
from graph.engine import Engine, GraphPatch, GraphStore
from graph.outcome import ANSWERED, ERROR, ESCALATED, SKIPPED, NodeOutcome
from graph.planner import RulePlanner
from graph.registry import RunContext
from llm import narrate
from transport.llm_client import LLMError
from transport.mcp_client import FORBIDDEN, TRANSIENT, MCPToolError

TODAY = dt.date(2026, 10, 5)
ITEM_ID = "e7e982cd-dd11-4f31-b912-3f0a9903f6ed"


def ids(tasks):
    return [t.id for t in tasks]


def by_id(tasks):
    return {t.id: t for t in tasks}


# --- plans.pipeline_review ----------------------------------------------------

def test_full_plan_shape():
    tasks = plans.pipeline_review(list(plans.ASKS), item_ref="vice", qty=5)
    assert ids(tasks) == ["snapshot_deals", "closing_this_month", "at_risk",
                          "resolve_item", "price_lookup", "build_finding", "narrate"]
    t = by_id(tasks)
    assert t["build_finding"].needs == "done"
    assert t["build_finding"].deps == ["closing_this_month", "at_risk", "resolve_item", "price_lookup"]
    assert t["narrate"].deps == ["build_finding"]
    assert t["price_lookup"].args == {"item": "resolve_item", "qty": 5}


def test_quote_only_plan_reads_no_deals():
    assert "snapshot_deals" not in ids(plans.pipeline_review(["quote"], item_ref="vice", qty=5))


def test_one_list_only():
    tasks = plans.pipeline_review(["at_risk"])
    assert ids(tasks) == ["snapshot_deals", "at_risk", "build_finding", "narrate"]


@pytest.mark.parametrize("item_ref, qty, missing", [
    ("vice", None, "how many units"),
    ("vice", 0, "how many units"),
    (None, 5, "which item"),
    (None, None, "which item and how many units"),
])
def test_quote_missing_item_or_qty_is_refused_at_planning(item_ref, qty, missing):
    t = by_id(plans.pipeline_review(["quote"], item_ref=item_ref, qty=qty))
    assert "resolve_item" not in t
    assert t["quote_refused"].node == "refuse"
    assert t["quote_refused"].args["reason"] == f"can't quote without knowing {missing}"
    assert "quote_refused" in t["build_finding"].deps


def test_out_of_scope_parts_become_refusal_nodes():
    t = by_id(plans.pipeline_review(["at_risk"], out_of_scope=[
        {"request": "pay my commission", "reason": "payroll"},
        {"request": "delete deal 9", "reason": "no deletes"}]))
    assert t["refuse_1"].args == {"request": "pay my commission", "reason": "payroll"}
    assert t["build_finding"].args["refusals"] == ["refuse_1", "refuse_2"]
    assert {"refuse_1", "refuse_2"} <= set(t["build_finding"].deps)


def test_other_request_is_recorded_as_not_handled():
    t = by_id(plans.pipeline_review(["at_risk"], other="who owns deal 42?"))
    assert t["build_finding"].args["not_handled"] == {
        "request": "who owns deal 42?", "reason": plans.NOT_HANDLED_REASON}


def test_unknown_ask_rejected():
    with pytest.raises(ValueError, match="unknown asks"):
        plans.pipeline_review(["at_risk", "forecast"])


def test_every_plan_node_is_in_the_registry():
    store = GraphStore("t", None)
    store.apply(GraphPatch(add=plans.pipeline_review(
        list(plans.ASKS), item_ref="vice", qty=5,
        out_of_scope=[{"request": "x", "reason": "y"}])))
    assert len(store.specs) == 8


# --- RulePlanner --------------------------------------------------------------

def store_for(asks, **kw):
    store = GraphStore("t", None)
    store.apply(GraphPatch(add=plans.pipeline_review(asks, **kw)))
    return store


def finish(store, node_id, outcome):
    spec = store.get(node_id)
    spec.outcome = outcome
    return spec


def test_unpriced_quote_gets_an_escalation_node():
    store = store_for(["quote"], item_ref="vice", qty=5)
    spec = finish(store, "price_lookup", NodeOutcome.from_domain({"outcome": "unpriced"}))

    patch = RulePlanner().on_outcome(store, spec)

    assert ids(patch.add) == ["escalate_quote"]
    assert patch.add[0].deps == ["price_lookup"]
    assert patch.add[0].args == {"priced": "price_lookup"}
    assert patch.connect == [("escalate_quote", "build_finding")]


def test_escalation_is_added_only_once():
    store = store_for(["quote"], item_ref="vice", qty=5)
    spec = finish(store, "price_lookup", NodeOutcome.from_domain({"outcome": "unpriced"}))
    store.apply(RulePlanner().on_outcome(store, spec))
    assert RulePlanner().on_outcome(store, spec) is None


def test_escalation_does_not_connect_into_a_started_finding():
    store = store_for(["quote"], item_ref="vice", qty=5)
    store.get("build_finding").started_at = 1.0
    spec = finish(store, "price_lookup", NodeOutcome.from_domain({"outcome": "unpriced"}))
    assert RulePlanner().on_outcome(store, spec).connect == []


@pytest.mark.parametrize("outcome", [
    NodeOutcome.from_domain({"outcome": "quoted"}),
    NodeOutcome.from_domain({"outcome": "refused", "reason": "bad qty"}),
])
def test_priced_or_refused_quote_needs_nothing(outcome):
    store = store_for(["quote"], item_ref="vice", qty=5)
    assert RulePlanner().on_outcome(store, finish(store, "price_lookup", outcome)) is None


def test_incomplete_snapshot_gets_one_reread_feeding_both_lists():
    store = store_for(["closing_this_month", "at_risk"])
    spec = finish(store, "snapshot_deals",
                  NodeOutcome(ANSWERED, data={"outcome": "answered", "pagination_complete": False}))

    patch = RulePlanner().on_outcome(store, spec)

    assert ids(patch.add) == ["reread_deals"]
    assert patch.add[0].args == {"previous": "snapshot_deals"}
    assert patch.connect == [("reread_deals", "closing_this_month"), ("reread_deals", "at_risk")]


def test_complete_snapshot_needs_no_reread():
    store = store_for(["at_risk"])
    spec = finish(store, "snapshot_deals",
                  NodeOutcome(ANSWERED, data={"outcome": "answered", "pagination_complete": True}))
    assert RulePlanner().on_outcome(store, spec) is None


# --- Engine: whole runs against a fake platform -------------------------------

def deal(id, stage="proposal", close=None, value=100.0):
    return {"id": id, "title": f"Deal {id}", "stage": stage, "expected_close_date": close,
            "value": value, "currency": "INR"}


DEALS = [deal("late", close="2026-10-01", value=50000.0),
         deal("later", close="2026-10-30", value=20000.0),
         deal("won", stage="closed_won", close="2026-10-10")]


def deal_pages(args):
    return {"data": DEALS[args["offset"]:args["offset"] + args["limit"]], "total": len(DEALS)}


def item(standard_rate=None):
    return {"id": ITEM_ID, "name": "SuryaTools Bench Vice 100mm", "code": "ST-VICE-100",
            "uom": "Nos", "default_bom_id": None, "standard_rate": standard_rate,
            "selling_price": 3499.0}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def no_llm(*a, **kw):
        raise LLMError("offline")
    monkeypatch.setattr(narrate, "call_llm", no_llm)
    monkeypatch.setattr(engine, "RETRY_BACKOFF_S", 0)


def run(client, asks=plans.ASKS, dry_run=True, **kw):
    ctx = RunContext(client=client, run_id="r1", session_id="s1", query="q",
                     dry_run=dry_run, today=TODAY)
    tasks = plans.pipeline_review(list(asks), **kw)
    return Engine(ctx, RulePlanner()).run("r1", tasks)


def finding(store):
    return store.get("build_finding").outcome.data


def test_composite_dry_run_escalates_without_filing(fake_client):
    client = fake_client({"Deal.list": deal_pages,
                          "Item.list": lambda a: {"data": [item()], "total": 1},
                          "AgentEscalation.list": lambda a: {"data": []}})

    store = run(client, item_ref="ST-VICE-100", qty=500)

    assert list(store.specs)[-1] == "escalate_quote"
    assert store.get("escalate_quote").added_by == "planner"
    assert store.get("escalate_quote").outcome.status == ESCALATED
    assert all(s.outcome is not None for s in store.specs.values())
    f = finding(store)
    assert f["closing_this_month"]["deal_ids"] == ["late", "later"]
    assert f["at_risk"]["deal_ids"] == ["late"]
    assert f["quote"]["outcome"] == "escalated" and f["quote"]["dry_run"] is True
    assert "AgentEscalation.create" not in client.tools_called()


def test_narration_falls_back_to_the_template_when_llm_is_down(fake_client):
    client = fake_client({"Deal.list": deal_pages,
                          "Item.list": lambda a: {"data": [item()], "total": 1},
                          "AgentEscalation.list": lambda a: {"data": []}})
    out = run(client, item_ref="ST-VICE-100", qty=500).get("narrate").outcome.data
    assert out["source"] == "template"
    assert out["fallback_reason"] == "offline"
    assert "Dry run" in out["text"]


def test_priced_item_is_quoted_and_never_escalated(fake_client):
    client = fake_client({"Item.list": lambda a: {"data": [item(standard_rate=10)], "total": 1}})
    store = run(client, asks=["quote"], item_ref="ST-VICE-100", qty=500)
    assert "escalate_quote" not in store
    assert finding(store)["quote"]["total_price"] == 5000.0


def test_transient_read_is_retried(fake_client):
    client = fake_client({"Deal.list": [MCPToolError("blip", code=TRANSIENT),
                                        deal_pages({"offset": 0, "limit": 50})]})
    store = run(client, asks=["at_risk"])
    snap = store.get("snapshot_deals")
    assert snap.attempts == 2 and snap.outcome.status == ANSWERED
    assert finding(store)["at_risk"]["deal_ids"] == ["late"]


def test_read_that_keeps_failing_reports_the_error_in_the_finding(fake_client):
    client = fake_client({"Deal.list": lambda a: MCPToolError("down", code=TRANSIENT)})
    store = run(client, asks=["at_risk"])

    snap = store.get("snapshot_deals")
    assert snap.attempts == 3  # 1 + registry retries=2
    assert snap.outcome.status == ERROR and snap.outcome.error_code == TRANSIENT
    assert store.get("at_risk").outcome.status == SKIPPED
    assert finding(store)["at_risk"]["outcome"] == "error"
    assert store.get("narrate").outcome.status == ANSWERED


def test_non_transient_error_is_not_retried(fake_client):
    client = fake_client({"Deal.list": lambda a: MCPToolError("no", code=FORBIDDEN)})
    snap = run(client, asks=["at_risk"]).get("snapshot_deals")
    assert snap.attempts == 1 and snap.outcome.error_code == FORBIDDEN


def test_a_write_is_never_retried(fake_client):
    client = fake_client({"Item.list": lambda a: {"data": [item()], "total": 1},
                          "AgentEscalation.list": lambda a: {"data": []},
                          "AgentEscalation.create": lambda a: MCPToolError("timeout", code=TRANSIENT)})
    esc = run(client, asks=["quote"], dry_run=False, item_ref="ST-VICE-100",
              qty=500).get("escalate_quote")
    assert esc.attempts == 1
    assert esc.outcome.status == ERROR
    assert client.tools_called().count("AgentEscalation.create") == 1


def test_a_bug_in_a_node_is_recorded_and_other_branches_still_answer(fake_client):
    def broken(args):
        raise RuntimeError("bad row")
    client = fake_client({"Deal.list": deal_pages, "Item.list": broken})

    store = run(client, item_ref="vice", qty=5)

    resolve = store.get("resolve_item").outcome
    assert resolve.status == ERROR and resolve.error_code == "internal"
    assert resolve.reason == "RuntimeError: bad row"
    assert finding(store)["at_risk"]["deal_ids"] == ["late"]
