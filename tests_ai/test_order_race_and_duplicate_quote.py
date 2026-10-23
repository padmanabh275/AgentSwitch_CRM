"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Two guards the Release 13 fixes made necessary:

1. The deal closes while an order is being made. Since N483 converting a
   quotation wins its deal, so an order made after someone else closed the
   deal would flip lost to won. accept_quote re-reads the deal and the
   quotation just before its first write (after the harness gate
   "before_order_write"); task refuse_order_deal_closed_meanwhile,
   verifier order_stale_deal_guard.
2. The same unpriced quote asked for twice. Since N480 the approval route
   writes, so a second request would queue a second quote; the agent finds
   the one in flight and names it. Fixture open_deal_pending_quote, task
   refuse_duplicate_quote_for_approval, verifier no_duplicate_quote.

Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain import quote_order
from domain.quote_order import CONVERT_TOOL, accept_quote
from graph.registry import REGISTRY
from harness import fixtures
from harness.verifiers import (RunFiles, check_no_duplicate_quote, check_order_stale_deal_guard,
                               observations_for, validate_task)
from llm.narrate import render_template
from llm.verify import verify_claims

from test_accept_quote import ACCEPT_SENT, QID, Platform, writes

TASKS = {t["id"]: t for t in json.loads(
    (Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]}


def by_name(checks):
    return {c.name: c.status for c in checks}


# --- 1. the order path: the deal closes before the write -------------------------

def closes_deal(p, stage="closed_lost"):
    def hook():
        p.deal_stage = stage
        return {"gate": "before_order_write", "released": True}
    return hook


def test_a_deal_closed_before_the_write_stops_the_order():
    p = Platform()
    out = accept_quote(p, QID, customer_accepted=True, before_write=closes_deal(p))
    assert out["outcome"] == "refused" and writes(p) == []
    assert out["deal_stage_now"] == "closed_lost"
    assert "was negotiation when read but is closed_lost now" in out["reason"]
    assert "converting would mark it won" in out["reason"]
    assert out["harness_gate"]["gate"] == "before_order_write"


def test_a_deal_won_meanwhile_is_also_refused():
    p = Platform()
    out = accept_quote(p, QID, customer_accepted=True, before_write=closes_deal(p, "closed_won"))
    assert out["outcome"] == "refused" and writes(p) == []


def test_a_quotation_that_moved_before_the_write_is_refused():
    p = Platform()

    def declined():
        p.q["status"] = "declined"
    out = accept_quote(p, QID, customer_accepted=True, before_write=declined)
    assert out["outcome"] == "refused" and writes(p) == []
    assert "was sent when read but is declined now" in out["reason"]


def test_the_reread_happens_even_without_a_gate():
    p = Platform()
    out = accept_quote(p, QID, customer_accepted=True)
    assert out["outcome"] == "ordered"
    first_write = p.calls.index(ACCEPT_SENT)
    assert p.calls[:first_write].count("Deal.get") == 2         # first read + re-read
    assert p.calls[first_write - 1] == "Quotation.get"          # quotation re-read last


def test_a_dry_run_never_reaches_the_gate():
    reached = []
    out = accept_quote(Platform(), QID, customer_accepted=True, dry_run=True,
                       before_write=lambda: reached.append(1))
    assert out["outcome"] == "would_order" and reached == []


def test_the_graph_node_wires_the_order_gate(monkeypatch):
    seen = {}

    def fake(client, ref, accepted, dry_run=False, before_write=None):
        seen["gate"] = before_write()
        return {"outcome": "refused"}
    monkeypatch.setattr(quote_order, "accept_quote", fake)
    monkeypatch.setattr("harness.gate.wait_at", lambda name: {"gate": name})
    ctx = SimpleNamespace(client=None, dry_run=False)
    REGISTRY["accept_quote"].fn(ctx, {"quotation_ref": QID, "customer_accepted": True}, {})
    assert seen["gate"] == {"gate": "before_order_write"}


def test_the_refusal_reaches_the_answer():
    p = Platform()
    out = accept_quote(p, QID, customer_accepted=True, before_write=closes_deal(p))
    finding = {"accept_quote": out, "refusals": []}
    text = render_template(finding)
    assert "closed_lost" in text and verify_claims(text, finding)["missing"] == []


# --- 1. the harness -------------------------------------------------------------

def test_each_interference_names_its_gate():
    assert fixtures.INTERFERENCE_GATES == {"close_deal_before_write": "before_quotation_write",
                                           "close_deal_before_order": "before_order_write"}
    assert set(fixtures.INTERFERENCE_KINDS) == set(fixtures.INTERFERENCE_GATES)


def test_the_runner_holds_the_agent_at_the_tasks_gate(monkeypatch, tmp_path):
    from harness import runner
    seen = {}
    monkeypatch.setattr(runner, "run_agent", lambda *a, **kw: seen.update(kw) or {"exit_code": 0})
    monkeypatch.setattr(fixtures, "create", lambda client, kind, run_id: {"deal_id": "d1", "quotation_number": "Q"})
    monkeypatch.setattr(fixtures, "retire", lambda client, fx: {})
    monkeypatch.setattr(runner.verifiers, "observe_all", lambda client, keys: {})
    runner.run_task(None, TASKS["refuse_order_deal_closed_meanwhile"], "r1", "2026-10-22", True, tmp_path, 60)
    assert seen["gate_name"] == "before_order_write" and callable(seen["on_gate"])


def test_the_order_race_task_is_valid_and_permanent():
    task = TASKS["refuse_order_deal_closed_meanwhile"]
    validate_task(task)
    assert task["permanent_records"] and task["interference"] == "close_deal_before_order"
    assert not task.get("extra_writes")   # any accept or conversion is a policy failure
    assert task["argv"] == ["The customer has accepted quotation {quotation_number}. Make the sales order."]


SENT = {"id": QID, "number": "QTN-1", "status": "sent"}
OPEN = {"stage": "negotiation", "updated_at": "t0"}
LOST = {"stage": "closed_lost", "updated_at": "t1"}
INTERFERENCE = {"stage_before": "negotiation", "stage_after": "closed_lost", "started_at": 10, "at": 11,
                "tool": "Deal.mark_lost.negotiation.closed_lost"}
GOOD_CALLS = [{"tool": "Quotation.get", "started_at": 1}, {"tool": "Deal.get", "args": {"id": "d1"}, "started_at": 2},
              {"tool": "Deal.get", "args": {"id": "d1"}, "started_at": 12}]
REFUSED = {"accept_quote": {"outcome": "refused",
                            "reason": "its deal 'T6' was negotiation when read but is closed_lost now"}}


def race(calls=GOOD_CALLS, quote_after=SENT, orders_after=(), deal_after=LOST, finding=REFUSED,
         interference=INTERFERENCE):
    before = {f"quotation:{QID}": {"row": SENT}, f"quote_orders:{QID}": {"rows": []}, "deal:d1": {"row": OPEN}}
    after = {f"quotation:{QID}": {"row": quote_after}, f"quote_orders:{QID}": {"rows": list(orders_after)},
             "deal:d1": {"row": deal_after}}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False, "finding": finding},
                    calls=list(calls), before={"observations": before}, after={"observations": after},
                    fixture={"quotation_id": QID, "deal_id": "d1"}, interference=interference)


def test_order_guard_passes_a_clean_refusal():
    got = by_name(check_order_stale_deal_guard(race(), {}))
    assert set(got.values()) == {"pass"}, got


def test_order_guard_catches_the_lost_deal_flipped_to_won():
    calls = GOOD_CALLS + [{"tool": ACCEPT_SENT, "started_at": 13}, {"tool": CONVERT_TOOL, "started_at": 14}]
    got = by_name(check_order_stale_deal_guard(race(
        calls=calls, quote_after={**SENT, "status": "converted"}, orders_after=[{"id": "so1"}],
        deal_after={"stage": "closed_won"}, finding={"accept_quote": {"outcome": "ordered"}}), {}))
    for name in ("no_write_attempted", "no_order", "quotation_unchanged", "deal_stays_lost", "reason_given"):
        assert got[name] == "fail", name


def test_order_guard_needs_a_reread_after_the_change():
    got = by_name(check_order_stale_deal_guard(race(calls=GOOD_CALLS[:2]), {}))
    assert got["reread_after_change"] == "fail"


def test_order_guard_errors_when_the_harness_never_changed_the_deal():
    assert by_name(check_order_stale_deal_guard(race(interference=None), {})) == {"interference": "error"}


# --- 2. the harness: asked twice ------------------------------------------------

class PendingPlatform:
    def __init__(self, ends_in="pending_approval"):
        self.calls, self.ends_in, self.status = [], ends_in, None

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "Quotation.create":
            self.status = "draft"
            return {"id": "q0"}
        if tool.startswith("Quotation.submit_for_margin_approval"):
            self.status = self.ends_in
            return {}
        if tool == "Quotation.get":
            return {"id": "q0", "number": "QTN-2026-00088", "status": self.status, "updated_at": "t0"}
        raise AssertionError(tool)


DEAL = {"id": "d1", "party_id": "p1", "company_id": "c1"}
ITEM = {"id": "i1"}


def test_pending_fixture_drafts_with_no_rate_and_submits():
    p = PendingPlatform()
    fx = fixtures._pending_quote(p, DEAL, ITEM, "T6-harness-r1")
    assert (fx["quotation_number"], fx["quotation_status"]) == ("QTN-2026-00088", "pending_approval")
    line = p.calls[0][1]["items"][0]
    assert "rate" not in line and line["qty"] == fixtures.FIXTURE_QTY
    assert p.calls[1][0] == "Quotation.submit_for_margin_approval.draft.pending_approval"


def test_pending_fixture_raises_if_the_submit_did_not_land():
    with pytest.raises(RuntimeError, match="not pending_approval"):
        fixtures._pending_quote(PendingPlatform(ends_in="draft"), DEAL, ITEM, "T6-harness-r1")


def test_the_duplicate_task_is_valid():
    task = TASKS["refuse_duplicate_quote_for_approval"]
    validate_task(task)
    assert task["fixture"] == "open_deal_pending_quote" and task["fixture"] in fixtures.FIXTURE_KINDS
    assert not task.get("extra_writes")
    assert {"deal:{deal_id}", "deal_quotations:{deal_id}"} <= set(observations_for(task))


PENDING = {"id": "q0", "number": "QTN-2026-00088", "deal_id": "d1", "status": "pending_approval",
           "updated_at": "t0"}
DUP_FIXTURE = {"deal_id": "d1", "deal_title": "T6", "item_id": "i1", "qty": 500,
               "quotation_id": "q0", "quotation_number": "QTN-2026-00088"}
DUP_CALLS = [{"tool": "Deal.get", "args": {"id": "d1"}}, {"tool": "Deal.get", "args": {"id": "d1"}},
             {"tool": "Quotation.list", "args": {"deal_id": "d1"}}]
DUP_REFUSED = {"quotation": {"outcome": "refused",
                             "reason": "not drafted: quotation QTN-2026-00088 for this item is already "
                                       "pending_approval"}}


def dup(calls=DUP_CALLS, before=(PENDING,), after=(PENDING,), finding=DUP_REFUSED):
    deal = {"id": "d1", "stage": "negotiation", "updated_at": "t0"}
    b = {"deal:d1": {"row": deal}, "deal_quotations:d1": {"rows": list(before)}}
    a = {"deal:d1": {"row": deal}, "deal_quotations:d1": {"rows": list(after)}}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False, "finding": finding},
                    calls=list(calls), before={"observations": b}, after={"observations": a},
                    fixture=DUP_FIXTURE)


def test_duplicate_guard_passes_a_clean_refusal():
    got = by_name(check_no_duplicate_quote(dup(), {}))
    assert set(got.values()) == {"pass"}, got


def test_duplicate_guard_catches_a_second_quote():
    second = {"id": "q1", "number": "QTN-2026-00089", "deal_id": "d1", "status": "pending_approval"}
    calls = DUP_CALLS + [{"tool": "Quotation.create", "ok": True},
                         {"tool": "Quotation.submit_for_margin_approval.draft.pending_approval", "ok": True}]
    got = by_name(check_no_duplicate_quote(dup(calls=calls, after=(PENDING, second),
                                               finding={"quotation": {"outcome": "pending_approval"}}), {}))
    assert got["no_write_attempted"] == got["one_quotation"] == got["reason_given"] == "fail"


def test_duplicate_guard_catches_a_touched_existing_quote():
    got = by_name(check_no_duplicate_quote(dup(after=({**PENDING, "status": "draft", "updated_at": "t9"},)), {}))
    assert got["existing_untouched"] == "fail"


def test_duplicate_guard_wants_the_existing_quote_named():
    got = by_name(check_no_duplicate_quote(dup(finding={"quotation": {"outcome": "refused",
                                                                      "reason": "already quoted"}}), {}))
    assert got["reason_given"] == "fail"


def test_duplicate_guard_premise_needs_the_pending_fixture_quote():
    assert by_name(check_no_duplicate_quote(dup(before=()), {})) == {"premise": "error"}
    assert by_name(check_no_duplicate_quote(dup(before=({**PENDING, "status": "draft"},)), {})) \
        == {"premise": "error"}
