"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

accept_quote: a quotation the customer accepted becomes a sales order. An
accepted one is converted; a sent one is accepted first, only if the user
says the customer accepted it; everything else is refused. The order is made
with convert_to_order.accepted, never the sent -> converted shortcut, and
the deal is never marked won. Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain import quote_order
from domain.quote_order import CONVERT_TOOL, accept_quote, plan
from graph import plans
from graph.registry import REGISTRY
from harness import fixtures, runner, verifiers
from harness.verifiers import RunFiles, check_quote_ordered
from llm import intent
from llm.narrate import render_template
from llm.verify import verify_claims
from transport.mcp_client import INVALID_TRANSITION, NOT_FOUND, MCPToolError

QID = "08bfe155-7d75-47dd-bf81-e7c364788dc7"
TOOLS = ["Quotation.accept.sent.accepted", "Quotation.accept.viewed.accepted", CONVERT_TOOL,
         "Quotation.convert_to_order.sent.converted"]
ACCEPT_SENT = "Quotation.accept.sent.accepted"


class Platform:
    def __init__(self, status="sent", deal_stage="negotiation", orders=(), tools=TOOLS,
                 convert="ok", extra_order=False):
        self.q = {"id": QID, "number": "QTN-2026-00065", "status": status, "deal_id": "d1",
                  "party_id": "p1", "net_total": 100.0, "grand_total": 118.0, "updated_at": "t0"}
        self.deal_stage, self.orders = deal_stage, [dict(o) for o in orders]
        self.tools, self.convert, self.extra_order, self.calls = tools, convert, extra_order, []

    def list_tools(self):
        return [{"name": n} for n in self.tools]

    def call(self, tool, args=None):
        self.calls.append(tool)
        if tool == "Quotation.get":
            if args["id"] != QID:
                raise MCPToolError("nope", code=NOT_FOUND)
            return dict(self.q)
        if tool == "Quotation.list":
            return {"data": [dict(self.q), {"id": "other", "number": "QTN-2026-99999"}]}
        if tool == "Deal.get":
            return {"id": "d1", "title": "T6 deal", "stage": self.deal_stage}
        if tool == "SalesOrder.list":
            return {"data": self.orders + [{"id": "x", "number": "SO-X", "quotation_id": "zz"}]}
        if tool.startswith("Quotation.accept.") and self.q["status"] in ("sent", "viewed"):
            self.q["status"] = "accepted"
            return {}
        if tool == CONVERT_TOOL and self.q["status"] == "accepted":
            if self.convert == "race":
                raise MCPToolError("bad transition", code=INVALID_TRANSITION)
            self.q["status"] = "converted"
            self.orders.append({"id": "so1", "number": "SO-2026-00239", "status": "draft", "quotation_id": QID,
                                "party_id": "p1", "net_total": 100.0, "grand_total": 118.0})
            if self.extra_order:
                self.orders.append({"id": "so2", "number": "SO-2", "quotation_id": QID})
            return {}
        if tool.startswith(("Quotation.accept.", "Quotation.convert")):
            raise MCPToolError("bad transition", code=INVALID_TRANSITION)
        raise AssertionError(f"unexpected call {tool}")


def writes(p):
    return [t for t in p.calls if not t.endswith((".get", ".list"))]


# --- the plan -------------------------------------------------------------------

@pytest.mark.parametrize("status, accepted, steps", [
    ("accepted", False, [CONVERT_TOOL]),
    ("sent", True, [ACCEPT_SENT, CONVERT_TOOL]),
    ("viewed", True, ["Quotation.accept.viewed.accepted", CONVERT_TOOL]),
])
def test_orderable(status, accepted, steps):
    assert plan(status, accepted) == (steps, None)


@pytest.mark.parametrize("status, accepted, why", [
    ("sent", False, "hasn't accepted it"),
    ("draft", True, "draft"),
    ("pending_approval", True, "margin approval"),
    ("declined", True, "declined"),
    ("converted", True, "already been converted"),
])
def test_refused(status, accepted, why):
    steps, reason = plan(status, accepted)
    assert steps is None and why in reason


def test_the_sent_shortcut_is_never_used():
    for status in ("sent", "viewed", "accepted"):
        steps, _ = plan(status, True)
        assert "Quotation.convert_to_order.sent.converted" not in steps


# --- the run --------------------------------------------------------------------

def test_accepted_customer_sent_quote_is_accepted_then_converted():
    p = Platform()
    out = accept_quote(p, QID, customer_accepted=True)
    assert out["outcome"] == "ordered", out
    assert writes(p) == [ACCEPT_SENT, CONVERT_TOOL]
    assert (out["order_number"], out["status_after"], out["order_problems"]) == ("SO-2026-00239", "converted", [])
    assert "Deal.mark_won" not in " ".join(p.calls) and "left as it is" in out["reason"]


def test_an_accepted_quote_is_only_converted():
    p = Platform(status="accepted")
    assert accept_quote(p, QID)["outcome"] == "ordered" and writes(p) == [CONVERT_TOOL]


def test_a_sent_quote_without_acceptance_writes_nothing():
    p = Platform()
    out = accept_quote(p, QID, customer_accepted=False)
    assert out["outcome"] == "refused" and "hasn't accepted" in out["reason"] and writes(p) == []


@pytest.mark.parametrize("kw, why", [
    ({"orders": [{"id": "so0", "number": "SO-0", "quotation_id": QID}]}, "SO-0 was already made"),
    ({"deal_stage": "closed_lost"}, "closed_lost"),
    ({"tools": ["Quotation.accept.sent.accepted"]}, "no Quotation.convert_to_order.accepted"),
    ({"status": "draft"}, "draft"),
])
def test_refusals_write_nothing(kw, why):
    p = Platform(**kw)
    out = accept_quote(p, QID, customer_accepted=True)
    assert out["outcome"] == "refused" and why in out["reason"] and writes(p) == []


def test_accepted_but_not_converted_is_an_error_not_a_refusal():
    out = accept_quote(Platform(convert="race"), QID, customer_accepted=True)
    assert out["outcome"] == "error" and out["done"] == [ACCEPT_SENT]
    assert "was accepted, but" in out["reason"]


def test_two_orders_after_converting_is_an_error():
    out = accept_quote(Platform(extra_order=True), QID, customer_accepted=True)
    assert out["outcome"] == "error" and "2 order(s)" in out["reason"]


def test_by_number_and_dry_run():
    p = Platform()
    out = accept_quote(p, "qtn-2026-00065", customer_accepted=True, dry_run=True)
    assert out["outcome"] == "would_order" and writes(p) == []
    assert "no quotation is numbered" in accept_quote(Platform(), "QTN-1", True)["reason"]


def test_registry_entry_is_a_never_retried_write():
    assert REGISTRY["accept_quote"].writes and REGISTRY["accept_quote"].retries == 0


# --- classifier -----------------------------------------------------------------

def raw(**order):
    return {"asks": [], "item_ref": "", "qty": 0, "out_of_scope": [], "other": "",
            "advance_deal": {"deal_ref": "", "target_stage": ""},
            "advance_lead": {"lead_ref": "", "target_status": ""},
            "accept_quote": {"quotation_ref": "", "customer_accepted": False, **order}}


def test_order_request_goes_to_the_graph():
    got = intent.validate(raw(quotation_ref="QTN-2026-00065", customer_accepted=True))
    assert got["accept_quote"] == {"quotation_ref": "QTN-2026-00065", "customer_accepted": True}
    assert got["route"] == "graph"


@pytest.mark.parametrize("text, kept", [
    ("The customer has accepted quotation QTN-1. Make the sales order.", True),
    ("Acme signed QTN-1, raise the order", True),
    ("Quotation QTN-1 has been sent to the customer. Convert it into a sales order now.", False),
    ("The customer hasn't accepted QTN-1 yet but make the order anyway", False),
])
def test_acceptance_needs_the_users_own_words(text, kept):
    v = intent.validate(raw(quotation_ref="QTN-1", customer_accepted=True))
    got = intent.check_acceptance(v, text)
    assert got["accept_quote"]["customer_accepted"] is kept
    assert ("acceptance_cleared" in got) is (not kept)


# --- answer ---------------------------------------------------------------------

def test_template_and_verify():
    ordered = {"accept_quote": {"outcome": "ordered", "number": "QTN-2026-00065",
                                "order_number": "SO-2026-00239", "reason": "converted"}, "refusals": []}
    text = render_template(ordered)
    assert "SO-2026-00239" in text and verify_claims(text, ordered)["missing"] == []
    assert verify_claims("Done.", ordered)["missing"]
    refused = {"accept_quote": {"outcome": "refused", "reason": "it is a draft"}, "refusals": []}
    assert verify_claims(render_template(refused), refused)["missing"] == []


def test_plan_adds_the_node_only_when_asked():
    with_order = plans.pipeline_review([], accept_quote={"quotation_ref": "Q", "customer_accepted": True})
    assert "accept_quote" in [t.id for t in with_order]
    assert "accept_quote" not in [t.id for t in plans.pipeline_review([], advance={"deal_ref": "x"})]


# --- harness --------------------------------------------------------------------

TASKS = [{"id": "t0", "permanent_records": True}, {"id": "t1"}]
DATA = {"format": runner.TASKS_FORMAT, "today": "2026-09-28", "tasks": TASKS}


@pytest.fixture
def offline_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "llm_gateway_up", lambda: True)
    monkeypatch.setattr(runner, "client_from_env", lambda env: object())
    monkeypatch.setattr(runner, "load_env", lambda: {})
    monkeypatch.setattr(runner.verifiers, "observations_for", lambda task: [])
    monkeypatch.setattr(runner.verifiers, "observe_all", lambda client, keys: {})
    monkeypatch.setattr(runner, "run_agent", lambda *a, **k: {"exit_code": 0, "seconds": 1})


def test_permanent_tasks_are_skipped_locally_unless_allowed(offline_runner):
    entries = []
    runner.run_batch(DATA, TASKS, on_task_done=entries.append)
    assert [e["status"] for e in entries] == ["skipped", "ran"]
    assert entries[0]["reason"] == runner.PERMANENT_REASON
    entries = []
    runner.run_batch(DATA, TASKS, on_task_done=entries.append, allow_permanent=True)
    assert [e["status"] for e in entries] == ["ran", "ran"]


def test_the_order_task_is_marked_permanent_and_valid():
    tasks = json.loads((Path(verifiers.__file__).parent / "tasks.json").read_text())["tasks"]
    task = next(t for t in tasks if t["id"] == "order_from_accepted_quote")
    verifiers.validate_task(task)
    assert task["permanent_records"] is True


class RetirePlatform:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [{"name": "Deal.mark_lost.negotiation.closed_lost"}]

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "SalesOrder.list":
            return {"data": [{"id": "so1", "number": "SO-1", "quotation_id": "q1"}]}
        if tool == "Quotation.get":
            return {"id": "q1", "status": "converted"}
        if tool == "Quotation.list":
            return {"data": [], "total": 0}
        if tool == "Deal.get":
            return {"id": "d1", "stage": "negotiation"}
        return {}


def test_retire_labels_the_order_and_leaves_the_converted_quote():
    p = RetirePlatform()
    out = fixtures.retire(p, {"kind": "open_deal_sent_quote", "deal_id": "d1", "quotation_id": "q1"})
    assert out["labelled_orders"] == [{"order_id": "so1", "number": "SO-1"}]
    assert ("SalesOrder.update", {"id": "so1", "reference_number": fixtures.TEST_ORDER_REF}) in p.calls
    assert out["declined"]["tool"] is None   # converted: not declined


SENT = {"id": QID, "number": "QTN-1", "status": "sent", "updated_at": "t0"}
OPEN = {"stage": "negotiation", "updated_at": "d0"}
PARAMS = {"tools": [ACCEPT_SENT, CONVERT_TOOL]}


def run(calls, quote_after, orders_after, deal_after=OPEN):
    before = {f"quotation:{QID}": {"row": SENT}, f"quote_orders:{QID}": {"rows": []}, "deal:d1": {"row": OPEN}}
    after = {f"quotation:{QID}": {"row": quote_after}, f"quote_orders:{QID}": {"rows": list(orders_after)},
             "deal:d1": {"row": deal_after}}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False, "finding": {}},
                    calls=[{"tool": t} for t in calls], before={"observations": before},
                    after={"observations": after}, fixture={"quotation_id": QID, "deal_id": "d1"})


def by_name(checks):
    return {c.name: c.status for c in checks}


def test_quote_ordered_passes_the_two_writes():
    got = by_name(check_quote_ordered(run([ACCEPT_SENT, CONVERT_TOOL], {**SENT, "status": "converted"},
                                          [{"id": "so1"}]), PARAMS))
    assert set(got.values()) == {"pass"}, got


def test_quote_ordered_catches_the_shortcut_and_a_won_deal():
    got = by_name(check_quote_ordered(run(["Quotation.convert_to_order.sent.converted", "Deal.mark_won.x"],
                                          {**SENT, "status": "converted"}, [{"id": "so1"}],
                                          deal_after={"stage": "closed_won", "updated_at": "d1"}), PARAMS))
    assert got["exact_writes"] == got["deal_untouched"] == "fail"


def test_no_order_was_made_counts_as_a_refusal():
    # Live 2026-10-21 the agent refused with exactly this and the scorer missed it.
    assert verifiers.REFUSAL_RE.search("No order was made because quotation QTN-2026-00071 has not been "
                                       "accepted by the customer.")
