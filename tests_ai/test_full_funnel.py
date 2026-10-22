"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

7. The whole funnel as one task (full_funnel_lead_to_order): one agent run
   per step, lead -> deal -> quote -> (a person's priced quote) -> order,
   each step scored with its own checks, and funnel_linked over the chain.
   Covers the runner's steps support, the quote_at_list_price hand-off, the
   funnel_lead fixture's clean-up, step scoring and validation.
And the N483 value-0 gap (order_on_value_zero_deal): the agent reports the
deal's value, and quote_ordered's value_zero mode passes while the gap
stands and errors once the platform wins such a deal.

Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.quote_order import accept_quote
from harness import fixtures, runner, score
from harness.run_record import read_json, write_json
from harness.verifiers import (RunFiles, check_funnel_linked, check_quote_ordered, check_run,
                               observations_for, step_task, validate_task)
from llm.narrate import render_template
from llm.verify import verify_claims

from test_accept_quote import ACCEPT_SENT, CONVERT_TOOL, QID, Platform
from test_accept_quote import run as order_run

TASKS = {t["id"]: t for t in json.loads(
    (Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]}
FUNNEL = TASKS["full_funnel_lead_to_order"]


def by_name(checks):
    return {c.name: c.status for c in checks}


# --- the task files -------------------------------------------------------------

def test_the_funnel_task_is_valid_and_walks_every_step():
    validate_task(FUNNEL)
    assert FUNNEL["permanent_records"] and FUNNEL["fixture"] == "funnel_lead"
    assert [s["name"] for s in FUNNEL["steps"]] == [
        "contact", "qualify", "convert", "deal_qualification", "deal_proposal", "deal_negotiation",
        "quote", "person_quotes_at_list_price", "order"]
    assert [s["extra_writes"] for s in FUNNEL["steps"] if "extra_writes" in s][:6] == [
        ["Lead.contact"], ["Lead.qualify"], ["Lead.convert"],
        ["Deal.qualify"], ["Deal.send_proposal"], ["Deal.negotiate"]]
    # the observations the chain needs exist only once the steps have named them
    assert observations_for(FUNNEL) == ["lead_state:{lead_id}", "lead_deals:{lead_id}", "deal:{deal_id}",
                                        "deal_quotations:{deal_id}", "quote_orders:{quotation_id}"]


def test_a_step_is_a_task_of_its_own():
    quote = next(s for s in FUNNEL["steps"] if s["name"] == "quote")
    t = step_task(FUNNEL, quote)
    assert t["id"] == "full_funnel_lead_to_order.quote" and t["step_of"] == FUNNEL["id"]
    assert t["requires_write"] and t["fixture"] == "funnel_lead" and t["argv"] == quote["argv"]
    assert "deal_quotations:{deal_id}" in observations_for(t) and "sessions_t6" in observations_for(t)


@pytest.mark.parametrize("change, match", [
    ({"argv": ["x"]}, "no argv or interference"),
    ({"fixture": None}, "needs a fixture"),
    ({"steps": [{"name": "a", "argv": []}, {"name": "a", "argv": []}]}, "its own name"),
    ({"steps": [{"name": "a", "harness": "approve_everything"}]}, "hand-off"),
    ({"steps": [{"name": "a", "argv": [], "interference": "close_deal_before_write"}]}, "takes only"),
    ({"steps": [{"name": "a", "argv": [], "verifiers": [{"name": "no_such"}]}]}, "unknown verifier"),
])
def test_bad_steps_are_rejected(change, match):
    with pytest.raises(ValueError, match=match):
        validate_task({**FUNNEL, **change})


def test_the_value_zero_task_is_valid_and_permanent():
    task = TASKS["order_on_value_zero_deal"]
    validate_task(task)
    assert task["permanent_records"] and task["fixture"] == "open_deal_sent_quote_no_value"
    assert task["verifiers"][0]["params"]["value_zero"] is True
    assert task["fixture"] in fixtures.QUOTE_KINDS and task["fixture"] in fixtures.SENT_KINDS


# --- the runner -----------------------------------------------------------------

FINDINGS = {
    "convert": {"advance_lead": {"outcome": "advanced", "deal_id": "d1", "deal_title": "Deal from lead"}},
    "quote": {"quotation": {"outcome": "pending_approval", "quotation_id": "q1", "number": "QTN-1"}},
}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    seen = {"argv": {}, "retired": None, "handoff": None}

    def fake_agent(task, run_id, today, write, run_dir, timeout_s=0, argv=None, **kw):
        seen["argv"][task["step"]] = argv
        finding = FINDINGS.get(task["step"], {})
        write_json(run_dir / "taskrun.json", {"run_id": run_id, "ended": "done", "finding": finding})
        return {"exit_code": seen.get("fail", {}).get(task["step"], 0), "seconds": 1}

    def fake_handoff(client, kind, state):
        seen["handoff"] = (kind, dict(state))
        return {"kind": kind, "state": {"quotation_id": "q2", "quotation_number": "QTN-2",
                                        "agent_quotation_id": state["quotation_id"]}}

    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(runner, "run_agent", fake_agent)
    monkeypatch.setattr(fixtures, "create", lambda client, kind, run_id: {"kind": kind, "lead_id": "l1",
                                                                          "item_id": "i1"})
    monkeypatch.setattr(fixtures, "handoff", fake_handoff)
    monkeypatch.setattr(fixtures, "retire", lambda client, st: seen.update(retired=dict(st)) or {"ok": 1})
    monkeypatch.setattr(runner.verifiers, "observe_all", lambda client, keys: {"keys": list(keys)})
    return seen


def test_the_funnel_runs_each_step_with_what_the_last_one_made(harness, tmp_path):
    run_dir = tmp_path / "r1"
    out = runner.run_task(None, FUNNEL, "r1", "2026-10-22", True, run_dir, 600)
    assert out["exit_code"] == 0 and out["steps"] == 9
    argv = harness["argv"]
    assert argv["contact"] == ["--advance-lead", "l1", "--to-status", "contacted"]
    assert argv["deal_qualification"] == ["--advance", "d1", "--to-stage", "qualification"]
    assert argv["quote"] == ["--asks", "quote", "--item", "i1", "--qty", "500", "--deal", "d1"]
    # the order step gets the person's quote, which the hand-off put in the state
    assert argv["order"] == ["--order-quote", "QTN-2", "--customer-accepted"]
    assert harness["handoff"][0] == "quote_at_list_price" and harness["handoff"][1]["quotation_id"] == "q1"
    # each step has its own record, with the state it started from
    step = tmp_path / "r1.7-quote"
    assert read_json(step / "fixture.json")["deal_id"] == "d1"
    assert read_json(step / "task.json")["argv_resolved"] == argv["quote"]
    assert (step / "ground_truth_before.json").exists() and (step / "ground_truth_after.json").exists()
    # the task's own before-read had only the lead to go on; the after-read has the chain
    assert read_json(run_dir / "ground_truth_before.json")["keys"] == ["lead_state:l1", "lead_deals:l1"]
    assert "quote_orders:q2" in read_json(run_dir / "ground_truth_after.json")["keys"]
    steps = read_json(run_dir / "steps.json")
    assert [s["status"] for s in steps] == ["ran"] * 7 + ["done", "ran"]
    assert steps[2]["captured"] == {"deal_id": "d1", "deal_title": "Deal from lead"}
    assert (harness["retired"]["quotation_id"], harness["retired"]["agent_quotation_id"]) == ("q2", "q1")
    assert "retired" in read_json(run_dir / "fixture.json")


def test_a_failed_step_stops_the_funnel(harness, tmp_path):
    harness["fail"] = {"deal_proposal": 1}
    out = runner.run_task(None, FUNNEL, "r1", "2026-10-22", True, tmp_path / "r1", 600)
    steps = read_json(tmp_path / "r1" / "steps.json")
    assert out["exit_code"] == 1 and out["stopped"] == "step deal_proposal exited 1"
    assert [s["status"] for s in steps] == ["ran"] * 5 + ["not_run"] * 4
    assert harness["handoff"] is None and "order" not in harness["argv"]
    assert harness["retired"]["deal_id"] == "d1"   # what was made is still cleaned up


def test_a_finding_without_the_capture_stops_the_funnel(harness, tmp_path, monkeypatch):
    monkeypatch.setitem(FINDINGS, "convert", {"advance_lead": {"outcome": "refused"}})
    out = runner.run_task(None, FUNNEL, "r1", "2026-10-22", True, tmp_path / "r1", 600)
    assert out["stopped"] == "step convert's finding has no deal_id, deal_title"
    assert "deal_qualification" not in harness["argv"]


# --- the hand-off and clean-up ---------------------------------------------------

class PersonPlatform:
    """The deal and the agent's quote waiting for approval; quotation writes
    as the platform makes them."""
    def __init__(self, deal_value=0, send_ok=True):
        self.deal = {"id": "d1", "party_id": "p1", "company_id": "c1", "stage": "negotiation",
                     "value": deal_value}
        self.quotes = {"q1": {"id": "q1", "number": "QTN-1", "status": "pending_approval"}}
        self.send_ok, self.calls = send_ok, []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "Quotation.get":
            return dict(self.quotes[args["id"]])
        if tool == "Deal.get":
            return dict(self.deal)
        if tool == "Deal.update":
            self.deal["value"] = args["value"]
        elif tool == "Quotation.create":
            self.quotes["q2"] = {"id": "q2", "number": "QTN-2", "status": "draft", "items": args["items"]}
            return {"id": "q2"}
        elif tool == "Quotation.send" and self.send_ok:
            self.quotes[args["id"]]["status"] = "sent"
        return {}


STATE = {"quotation_id": "q1", "quotation_number": "QTN-1", "deal_id": "d1", "item_id": "i1",
         "list_rate": 10.0, "qty": 500, "lead_title": "T6-harness-r1"}


def test_the_person_quotes_the_item_at_list_price_and_sends_it():
    p = PersonPlatform()
    out = fixtures.handoff(p, "quote_at_list_price", STATE)
    writes = [t for t, _ in p.calls if not t.endswith(".get")]
    assert writes == ["Deal.update", "Quotation.create", "Quotation.send"]
    line = dict(p.calls)["Quotation.create"]["items"][0]
    assert (line["item_id"], line["qty"], line["rate"]) == ("i1", 500, 10.0)
    assert (out["deal_value_before"], out["deal_value_after"]) == (0, 5000.0)
    assert out["agent_quotation_status"] == "pending_approval"
    assert p.quotes["q1"]["status"] == "pending_approval"   # the agent's is left waiting
    st = out["state"]
    assert (st["quotation_id"], st["quotation_number"], st["quotation_status"]) == ("q2", "QTN-2", "sent")
    assert (st["agent_quotation_id"], st["agent_quotation_number"]) == ("q1", "QTN-1")


def test_a_deal_with_a_value_keeps_it():
    p = PersonPlatform(deal_value=7000)
    fixtures.handoff(p, "quote_at_list_price", STATE)
    assert "Deal.update" not in [t for t, _ in p.calls]


def test_a_quote_the_platform_did_not_send_stops_the_hand_off():
    p = PersonPlatform(send_ok=False)
    with pytest.raises(RuntimeError, match="not sent"):
        fixtures.handoff(p, "quote_at_list_price", STATE)


class CleanupPlatform:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [{"name": n} for n in ("Quotation.decline.sent.declined", "Deal.mark_lost.negotiation.closed_lost",
                                      "Lead.disqualify.qualified.disqualified")]

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "SalesOrder.list":
            return {"data": [{"id": "so1", "number": "SO-1", "quotation_id": "q1"}]}
        if tool == "Quotation.get":
            return {"id": "q1", "status": "converted"}
        if tool == "Quotation.list":
            return {"data": [], "total": 0}
        if tool == "Deal.list":
            return {"data": [{"id": "d1", "lead_id": "l1"}]}
        if tool == "Deal.get":
            return {"id": "d1", "stage": "closed_won"}
        if tool == "Lead.get":
            return {"id": "l1", "status": "converted"}
        return {}


def test_retiring_a_funnel_labels_its_order_and_leaves_the_won_deal():
    p = CleanupPlatform()
    out = fixtures.retire(p, {"kind": "funnel_lead", "lead_id": "l1", "deal_id": "d1", "quotation_id": "q1"})
    assert out["quote"]["labelled_orders"] == [{"order_id": "so1", "number": "SO-1"}]
    assert ("SalesOrder.update", {"id": "so1", "reference_number": fixtures.TEST_ORDER_REF}) in p.calls
    assert out["closed_deals"][0]["stage_after"] == "closed_won" and out["status_after"] == "converted"
    assert not any(t.startswith(("Deal.mark_", "Lead.disqualify")) for t, _ in p.calls)


def test_retiring_a_funnel_that_stopped_before_the_quote_closes_the_lead():
    p = CleanupPlatform()
    out = fixtures.retire(p, {"kind": "funnel_lead", "lead_id": "l1"})
    assert "quote" not in out and not any(t == "SalesOrder.list" for t, _ in p.calls)


# --- scoring the chain ---------------------------------------------------------------

ME = "me1"
CHAIN = {"lead_state:l1": {"row": {"id": "l1", "status": "converted"}},
         "lead_deals:l1": {"rows": [{"id": "d1", "lead_id": "l1"}]},
         "deal:d1": {"row": {"id": "d1", "stage": "closed_won", "value": 5000}},
         "deal_quotations:d1": {"rows": [
             {"id": "q1", "number": "QTN-1", "status": "pending_approval", "created_by": ME},
             {"id": "q2", "number": "QTN-2", "status": "converted", "created_by": ME}]},
         "quote_orders:q2": {"rows": [{"id": "so1", "number": "SO-1", "quotation_id": "q2", "deal_id": "d1"}]}}
ALL_RAN = [{"step": s["name"], "status": "done" if s.get("harness") else "ran"} for s in FUNNEL["steps"]]


def funnel_files(after=None, steps=ALL_RAN, fixture=None):
    return RunFiles(run_dir=Path("."), task=FUNNEL, taskrun={"run_id": "r1"}, graph=None, chat_trace=None,
                    calls=[], before={}, after={"observations": {**CHAIN, **(after or {})}},
                    fixture=fixture or {"lead_id": "l1", "deal_id": "d1", "deal_title": "D",
                                        "agent_quotation_id": "q1", "agent_quotation_number": "QTN-1",
                                        "quotation_id": "q2", "quotation_number": "QTN-2", "me": ME},
                    steps=steps)


def test_a_linked_chain_passes():
    got = by_name(check_funnel_linked(funnel_files(), {}))
    assert set(got.values()) == {"pass"} and len(got) == 6, got


@pytest.mark.parametrize("after, check", [
    ({"lead_deals:l1": {"rows": [{"id": "d1"}, {"id": "d2"}]}}, "lead_to_deal"),
    ({"deal_quotations:d1": {"rows": [{"id": "q1", "status": "pending_approval", "created_by": ME},
                                      {"id": "q2", "status": "converted", "created_by": ME},
                                      {"id": "q3", "status": "draft", "created_by": ME}]}}, "deal_to_quote"),
    ({"deal_quotations:d1": {"rows": [{"id": "q1", "status": "pending_approval", "created_by": ME},
                                      {"id": "q2", "status": "sent", "created_by": ME}]}}, "deal_to_quote"),
    ({"deal_quotations:d1": {"rows": [{"id": "q1", "status": "draft", "created_by": ME},
                                      {"id": "q2", "status": "converted", "created_by": ME}]}}, "agent_quote_waiting"),
    ({"quote_orders:q2": {"rows": [{"id": "so1", "deal_id": None}]}}, "quote_to_order"),
])
def test_a_broken_link_fails(after, check):
    assert by_name(check_funnel_linked(funnel_files(after), {}))[check] == "fail"


def test_a_deal_the_platform_did_not_win_is_the_platforms_error():
    got = by_name(check_funnel_linked(funnel_files({"deal:d1": {"row": {"id": "d1", "stage": "negotiation"}}}), {}))
    assert got["deal_won"] == "error"


def test_a_funnel_that_stopped_says_where():
    steps = ALL_RAN[:5] + [{"step": "deal_negotiation", "status": "ran"},
                           {"step": "quote", "status": "ran"},
                           {"step": "person_quotes_at_list_price", "status": "error", "reason": "boom"},
                           {"step": "order", "status": "not_run", "reason": "hand-off failed"}]
    fx = {"lead_id": "l1", "deal_id": "d1", "agent_quotation_id": "q1", "me": ME}
    checks = check_funnel_linked(funnel_files(steps=steps, fixture=fx), {})
    got = {c.name: c for c in checks}
    assert got["every_step_ran"].status == "fail"
    assert "person_quotes_at_list_price (error: boom)" in got["every_step_ran"].detail
    assert got["chain"].status == "fail" and "quotation_id" in got["chain"].detail


def test_each_step_is_scored_as_its_own_run(tmp_path):
    step_dir = tmp_path / "r1.1-contact"
    task = step_task(FUNNEL, FUNNEL["steps"][0])
    write_json(step_dir / "task.json", task)
    write_json(step_dir / "taskrun.json", {"ended": "done", "dry_run": False,
                                           "finding": {"advance_lead": {"outcome": "advanced"}}})
    (step_dir / "calls.jsonl").write_text("")
    run_dir = tmp_path / "r1"
    write_json(run_dir / "task.json", FUNNEL)
    write_json(run_dir / "steps.json", [{"step": "contact", "status": "ran", "run_id": "r1.1-contact"}])
    write_json(run_dir / "fixture.json", {"lead_id": "l1", "me": ME})
    assert score.scorable(run_dir) and not (run_dir / "taskrun.json").exists()
    checks = check_run(RunFiles.load(run_dir))
    names = {c.name for c in checks}
    assert {"contact:ended", "contact:section:advance_lead", "contact:tools"} <= names
    assert by_name(checks)["contact:ended"] == "pass"
    assert by_name(checks)["every_step_ran"] == "fail"   # only one of the nine ran
    out = score.score_run(run_dir)
    assert out["run_id"] == "r1" and out["status"] == "fail"


# --- the N483 value-0 gap ---------------------------------------------------------

def test_the_order_finding_carries_the_deal_value():
    class ZeroValue(Platform):
        def call(self, tool, args=None):
            out = super().call(tool, args)
            return {**out, "value": 0} if tool == "Deal.get" else out
    out = accept_quote(ZeroValue(pre_r13=True), QID, customer_accepted=True)
    assert out["deal_value"] == 0 and out["deal_stage_after"] == "negotiation"


VZ = {"tools": [ACCEPT_SENT, CONVERT_TOOL], "value_zero": True}
CONVERTED = {"id": QID, "number": "QTN-1", "status": "converted"}
ORDER = {"id": "so1", "number": "SO-1", "deal_id": "d1"}
OPEN_ZERO = {"stage": "negotiation", "updated_at": "d0"}
LEFT_OPEN = {"accept_quote": {"deal_stage_after": "negotiation", "deal_value": 0,
                              "reason": "...; deal 'D' is still negotiation (this agent doesn't mark deals "
                                        "won; its value is 0, and the platform won't mark a deal won "
                                        "without a value)"}}


TOLD = ("Sales order SO-1 made from quotation QTN-1. The deal is still at negotiation: its value is 0, "
        "and the platform doesn't mark a deal won without a value.")


def value_zero_run(answer=TOLD, finding=LEFT_OPEN):
    files = order_run([ACCEPT_SENT, CONVERT_TOOL], CONVERTED, [ORDER], deal_after=OPEN_ZERO, finding=finding)
    files.taskrun["answer"] = answer
    return files


def test_value_zero_passes_while_the_gap_stands():
    got = by_name(check_quote_ordered(value_zero_run(), VZ))
    assert set(got.values()) == {"pass"}, got
    assert "deal_won_by_platform" not in got


def test_value_zero_fails_when_the_answer_leaves_it_out():
    # The live run of 2026-10-22 (batch 20261022T212902): the LLM's answer named
    # the order and nothing about the deal.
    got = by_name(check_quote_ordered(value_zero_run("Sales order SO-1 made from quotation QTN-1."), VZ))
    assert got["value_zero_answer"] == "fail" and got["value_zero_explained"] == "pass"


def test_verify_asks_for_the_open_deal_and_its_value():
    order = {"outcome": "ordered", "order_number": "SO-1", "number": "QTN-1",
             "deal_stage_after": "negotiation", "deal_value": 0}
    finding = {"accept_quote": order, "refusals": []}
    assert verify_claims("Sales order SO-1 made from quotation QTN-1.", finding)["missing"] == [
        "the deal is still negotiation; its value is 0, so the platform didn't mark it won"]
    assert verify_claims(TOLD, finding)["missing"] == []
    assert verify_claims("Made SO-1; the deal is still in negotiation.", finding)["missing"]   # no why
    valued = {"accept_quote": {**order, "deal_value": 900}, "refusals": []}
    assert verify_claims("Made SO-1; the deal is still in negotiation.", valued)["missing"] == []
    # the template says it all, so a fallback answer passes
    assert verify_claims(render_template({"accept_quote": {**order, "reason": LEFT_OPEN["accept_quote"]["reason"]},
                                          "refusals": []}), finding)["missing"] == []


def test_value_zero_errors_once_the_platform_wins_the_deal():
    won = {"accept_quote": {"deal_stage_after": "closed_won", "deal_value": 0}}
    checks = check_quote_ordered(order_run([ACCEPT_SENT, CONVERT_TOOL], CONVERTED, [ORDER],
                                           finding=won), VZ)
    c = next(c for c in checks if c.name == "value_zero_deal_left_open")
    assert c.status == "error" and "N483 gap is fixed" in c.detail


def test_value_zero_fails_when_the_agent_does_not_say_why():
    silent = {"accept_quote": {"deal_stage_after": "negotiation", "deal_value": 0, "reason": "converted it"}}
    got = by_name(check_quote_ordered(value_zero_run(finding=silent), VZ))
    assert got["value_zero_explained"] == "fail"


def test_value_zero_premise_needs_a_deal_without_a_value():
    files = order_run([ACCEPT_SENT, CONVERT_TOOL], CONVERTED, [ORDER], deal_after=OPEN_ZERO, finding=LEFT_OPEN)
    files.before["observations"]["deal:d1"] = {"row": {**OPEN_ZERO, "value": 900}}
    assert by_name(check_quote_ordered(files, VZ))["value_zero_premise"] == "error"
