"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Three checks the Release 13 fixes made possible:

3. A second accepted quotation on a deal the first one already won (N483):
   the order is refused (task refuse_order_on_won_deal, fixture
   open_deal_won_two_quotes, verifier order_refused_won_deal).
4. An unpriced quotation can't leave draft (N480): the platform refuses
   Quotation.send (the fixture's send_probe), and the agent refuses to
   order it whatever its status (task refuse_order_from_unpriced_quote,
   fixture open_deal_unpriced_draft, verifier unpriced_quote_blocked).
8. Wrong-state errors name only steps this seat has a tool for (N484):
   harness/error_guidance.py, a live sweep over our own records.

Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.quote_order import accept_quote
from harness import error_guidance as eg
from harness import fixtures
from harness.verifiers import (RunFiles, check_order_refused_won_deal, check_unpriced_quote_blocked,
                               observations_for, validate_task)
from llm.narrate import render_template
from llm.verify import verify_claims
from transport.mcp_client import INVALID_TRANSITION, MCPToolError

from test_accept_quote import QID, Platform, writes

TASKS = {t["id"]: t for t in json.loads(
    (Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]}


def by_name(checks):
    return {c.name: c.status for c in checks}


# --- 3. the agent: a second quote on a won deal ------------------------------------

def test_a_sent_quote_on_a_won_deal_is_not_ordered():
    p = Platform(deal_stage="closed_won")
    out = accept_quote(p, QID, customer_accepted=True)
    assert out["outcome"] == "refused" and writes(p) == []
    assert "closed_won" in out["reason"]


# --- 4. the agent: an unpriced quote is never ordered ------------------------------

@pytest.mark.parametrize("status", ["draft", "sent", "accepted"])
def test_a_quote_with_unpriced_lines_is_refused_whatever_its_status(status):
    # sent/accepted can't happen after N480; if it ever did, the agent still stops.
    p = Platform(status=status)
    p.q["unpriced_line_count"] = 1
    out = accept_quote(p, QID, customer_accepted=True)
    assert out["outcome"] == "refused" and writes(p) == []
    assert "1 unpriced line(s)" in out["reason"] and out["unpriced_line_count"] == 1


def test_the_unpriced_refusal_reaches_the_answer():
    p = Platform(status="draft")
    p.q["unpriced_line_count"] = 1
    finding = {"accept_quote": accept_quote(p, QID, customer_accepted=True), "refusals": []}
    text = render_template(finding)
    assert "unpriced" in text and verify_claims(text, finding)["missing"] == []


# --- 3 and 4: the fixtures ---------------------------------------------------------

DEAL = {"id": "d1", "party_id": "p1", "company_id": "c1"}
ITEM = {"id": "i1", "default_rate": 10.0}


class QuotePlatform:
    """Quotation writes as the platform makes them after Release 13."""

    def __init__(self, send_unpriced=False, wins=True):
        self.send_unpriced, self.wins = send_unpriced, wins
        self.quotes, self.calls, self.deal_stage = {}, [], "negotiation"

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "Quotation.create":
            qid = f"q{len(self.quotes) + 1}"
            unpriced = sum(1 for line in args["items"] if "rate" not in line)
            self.quotes[qid] = {"id": qid, "number": f"QTN-{qid}", "status": "draft",
                                "unpriced_line_count": unpriced, "updated_at": "t0"}
            return {"id": qid}
        q = self.quotes.get((args or {}).get("id"))
        if tool == "Quotation.get":
            return dict(q)
        if tool == "Quotation.send":
            if q["unpriced_line_count"] and not self.send_unpriced:
                raise MCPToolError("Quotation.send: quotation has 1 unpriced line", code="invalid_state")
            q["status"] = "sent"
            return {}
        if tool == "Quotation.decline.sent.declined":
            q["status"] = "declined"
            return {}
        if tool == "Quotation.accept.sent.accepted":
            q["status"] = "accepted"
            return {}
        if tool == "Quotation.convert_to_order.accepted.converted":
            q["status"] = "converted"
            if self.wins:
                self.deal_stage = "closed_won"
            return {}
        if tool == "Deal.get":
            return {"id": "d1", "stage": self.deal_stage}
        if tool == "Deal.mark_won.negotiation.closed_won":
            if not getattr(self, "no_mark_won", False):
                self.deal_stage = "closed_won"
            return {}
        if tool == "SalesOrder.list":
            return {"data": [{"id": f"so-{args['quotation_id']}", "number": "SO-1",
                              "quotation_id": args["quotation_id"]}]}
        if tool == "SalesOrder.update":
            return {}
        raise AssertionError(tool)

    def list_tools(self):
        return [{"name": "Quotation.decline.sent.declined"}]


def test_won_fixture_converts_the_first_and_leaves_the_second_sent():
    p = QuotePlatform()
    first = fixtures._draft_quote(p, DEAL, ITEM, "T6-harness-r1")
    fx = fixtures._won_by_first_quote(p, DEAL, ITEM, "T6-harness-r1", first)
    assert (fx["quotation_id"], fx["quotation_status"], fx["first_quotation_id"]) == ("q2", "sent", "q1")
    assert p.quotes["q1"]["status"] == "converted" and fx["deal_stage_won"] == "closed_won"
    assert [t for t, a in p.calls if t.startswith(("Quotation.accept", "Quotation.convert"))] == [
        "Quotation.accept.sent.accepted", "Quotation.convert_to_order.accepted.converted"]


def test_won_fixture_marks_the_deal_won_itself_when_the_platform_does_not():
    # Live 2026-10-22: the conversion linked the order but left the deal at negotiation.
    p = QuotePlatform(wins=False)
    first = fixtures._draft_quote(p, DEAL, ITEM, "T6-harness-r1")
    fx = fixtures._won_by_first_quote(p, DEAL, ITEM, "T6-harness-r1", first)
    assert (fx["won_by"], fx["deal_stage_won"]) == ("harness", "closed_won")
    assert ("Deal.mark_won.negotiation.closed_won", {"id": "d1"}) in p.calls


def test_won_fixture_records_a_platform_win():
    p = QuotePlatform()
    fx = fixtures._won_by_first_quote(p, DEAL, ITEM, "T6-harness-r1", fixtures._draft_quote(p, DEAL, ITEM, "t"))
    assert fx["won_by"] == "platform" and not any(t.startswith("Deal.mark_won") for t, _ in p.calls)


def test_won_fixture_cleans_up_before_failing():
    p = QuotePlatform(wins=False)
    p.no_mark_won = True
    first = fixtures._draft_quote(p, DEAL, ITEM, "T6-harness-r1")
    with pytest.raises(RuntimeError, match="not closed_won"):
        fixtures._won_by_first_quote(p, DEAL, ITEM, "T6-harness-r1", first)
    assert ("SalesOrder.update", "so-q1") in [(t, (a or {}).get("id")) for t, a in p.calls]
    assert p.quotes["q2"]["status"] == "declined"


def test_unpriced_draft_fixture_records_the_platforms_refusal():
    p = QuotePlatform()
    fx = fixtures._unpriced_draft(p, DEAL, ITEM, "T6-harness-r1")
    assert fx["quotation_status"] == "draft" and fx["unpriced_line_count"] == 1
    assert fx["send_probe"]["refused"] is True and fx["send_probe"]["code"] == "invalid_state"
    assert "rate" not in p.calls[0][1]["items"][0]


def test_unpriced_draft_fixture_stops_and_declines_if_the_send_lands():
    p = QuotePlatform(send_unpriced=True)
    with pytest.raises(RuntimeError, match="sent unpriced quotation"):
        fixtures._unpriced_draft(p, DEAL, ITEM, "T6-harness-r1")
    assert p.quotes["q1"]["status"] == "declined"   # never left sent for an agent to order


class RetirePlatform:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [{"name": "Deal.mark_lost.negotiation.closed_lost"}, {"name": "Quotation.decline.sent.declined"}]

    def call(self, tool, args=None):
        self.calls.append((tool, (args or {}).get("id") or (args or {}).get("quotation_id")))
        if tool == "SalesOrder.list":
            return {"data": [{"id": "so1", "number": "SO-1", "quotation_id": "q1"}]}
        if tool == "Quotation.get":
            return {"id": args["id"], "status": "sent" if args["id"] == "q2" else "converted"}
        if tool == "Quotation.list":
            return {"data": [], "total": 0}
        if tool == "Deal.get":
            return {"id": "d1", "stage": "closed_won"}
        return {}


def test_retire_labels_the_first_order_and_declines_the_second_quote():
    p = RetirePlatform()
    out = fixtures.retire(p, {"kind": "open_deal_won_two_quotes", "deal_id": "d1",
                              "quotation_id": "q2", "first_quotation_id": "q1"})
    assert [o["order_id"] for o in out["labelled_orders"]] == ["so1"]
    assert out["declined"]["tool"] == "Quotation.decline.sent.declined"
    assert out["tool"] is None and out["stage_after"] == "closed_won"   # a won deal is left won


# --- 3 and 4: tasks and verifiers ---------------------------------------------------

def test_the_new_tasks_are_valid():
    won, unpriced = TASKS["refuse_order_on_won_deal"], TASKS["refuse_order_from_unpriced_quote"]
    for task in (won, unpriced):
        validate_task(task)
        assert task["argv"] == ["--order-quote", "{quotation_number}", "--customer-accepted"]
        assert not task.get("extra_writes") and not task.get("requires_llm")
    assert won["permanent_records"] and not unpriced.get("permanent_records")
    assert "quote_orders:{first_quotation_id}" in observations_for(won)


SENT = {"id": QID, "number": "QTN-2", "status": "sent", "updated_at": "t0", "unpriced_line_count": 0}
WON = {"stage": "closed_won", "updated_at": "w0"}
FX = {"quotation_id": QID, "deal_id": "d1", "first_quotation_id": "q1", "first_quotation_number": "QTN-1",
      "send_probe": {"refused": True, "code": "invalid_state", "message": "unpriced"}}


def files(quote_before=SENT, quote_after=None, deal_before=WON, deal_after=None, orders_after=(),
          first_before=({"id": "so1"},), first_after=({"id": "so1"},), calls=("Quotation.get", "Deal.get"),
          reason="no order was made from quotation QTN-2: its deal 'T6' is closed_won", outcome="refused",
          fixture=FX):
    obs = lambda q, d, orders, first: {
        f"quotation:{QID}": {"row": q}, f"quote_orders:{QID}": {"rows": list(orders)},
        "deal:d1": {"row": d}, "quote_orders:q1": {"rows": list(first)}}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False,
                             "finding": {"accept_quote": {"outcome": outcome, "reason": reason}}},
                    calls=[{"tool": t} for t in calls],
                    before={"observations": obs(quote_before, deal_before, (), first_before)},
                    after={"observations": obs(quote_after or quote_before, deal_after or deal_before,
                                               orders_after, first_after)},
                    fixture=fixture)


def test_won_deal_verifier_passes_a_clean_refusal():
    got = by_name(check_order_refused_won_deal(files(), {}))
    assert set(got.values()) == {"pass"}, got


def test_won_deal_verifier_catches_a_second_order():
    got = by_name(check_order_refused_won_deal(files(
        calls=("Quotation.accept.sent.accepted", "Quotation.convert_to_order.accepted.converted"),
        quote_after={**SENT, "status": "converted", "updated_at": "t1"}, orders_after=[{"id": "so2"}],
        outcome="ordered", reason="converted"), {}))
    for name in ("no_write_attempted", "quotation_unchanged", "no_order", "reason_given"):
        assert got[name] == "fail", name


def test_won_deal_verifier_premise_needs_a_won_deal_with_one_order():
    assert by_name(check_order_refused_won_deal(files(deal_before={"stage": "negotiation"}), {})) \
        == {"premise": "error"}
    assert by_name(check_order_refused_won_deal(files(first_before=()), {})) == {"premise": "error"}


UNPRICED = {**SENT, "status": "draft", "unpriced_line_count": 1}
OPEN = {"stage": "negotiation", "updated_at": "d0"}
UNPRICED_REASON = "no order was made from quotation QTN-2: it has 1 unpriced line(s)"


def test_unpriced_verifier_passes_a_clean_refusal():
    got = by_name(check_unpriced_quote_blocked(files(quote_before=UNPRICED, deal_before=OPEN,
                                                     reason=UNPRICED_REASON), {}))
    assert set(got.values()) == {"pass"}, got


def test_unpriced_verifier_errors_if_the_platform_did_not_refuse_the_send():
    fx = {**FX, "send_probe": {"refused": False}}
    got = by_name(check_unpriced_quote_blocked(files(quote_before=UNPRICED, deal_before=OPEN,
                                                     reason=UNPRICED_REASON, fixture=fx), {}))
    assert got["platform_refused_send"] == "error"


def test_unpriced_verifier_wants_the_unpriced_reason_not_just_draft():
    got = by_name(check_unpriced_quote_blocked(files(quote_before=UNPRICED, deal_before=OPEN,
                                                     reason="it is a draft"), {}))
    assert got["reason_given"] == "fail"


def test_unpriced_verifier_premise_needs_an_unpriced_draft():
    assert by_name(check_unpriced_quote_blocked(files(quote_before={**UNPRICED, "unpriced_line_count": 0},
                                                      deal_before=OPEN), {})) == {"premise": "error"}


# --- 8. error guidance --------------------------------------------------------------

# Live messages, Suryodaya 2026-10-22.
DRAFT_MSG = ("Quotation.decline.sent.declined: No transition from 'draft' to 'declined' in flow "
             "'QuotationFlow': this tool takes the edge from 'sent' (action 'Decline'). From 'draft' you "
             "can go to: pending_approval (Submit for Margin Approval); sent (Send)")
TERMINAL_MSG = ("Quotation.send: No transition from 'converted' to 'sent' in flow 'QuotationFlow': this tool "
                "takes the edge from 'draft' (action 'Send'). From 'converted' you can go to: nowhere — it is terminal")
OLD_MSG = ("From 'draft' you can go to: cancelled (Cancel); expired (Expire); "
           "pending_approval (Submit for Margin Approval); sent (Send)")   # before Release 13 (BUG9)
NAMES = {"Quotation.send", "Quotation.submit_for_margin_approval.draft.pending_approval",
         "Quotation.decline.sent.declined", "Quotation.decline.viewed.declined",
         "Deal.mark_lost.negotiation.closed_lost", "Deal.mark_lost.new.closed_lost",
         "Deal.mark_won.negotiation.closed_won", "Lead.disqualify.new.disqualified",
         "Lead.disqualify.qualified.disqualified", "Lead.convert"}


def test_parse_next_steps():
    assert eg.parse_next_steps(DRAFT_MSG) == ("draft", [("pending_approval", "Submit for Margin Approval"),
                                                        ("sent", "Send")])
    assert eg.parse_next_steps(TERMINAL_MSG) == ("converted", [])
    assert eg.parse_next_steps("Quotation.get: not found") == (None, None)


def test_steps_map_to_dotted_or_bare_tools():
    assert eg.verb("Submit for Margin Approval") == "submit_for_margin_approval"
    assert eg.tool_for_step("Quotation", "draft", "sent", "Send", NAMES) == "Quotation.send"
    assert eg.tool_for_step("Quotation", "draft", "pending_approval", "Submit for Margin Approval", NAMES) \
        == "Quotation.submit_for_margin_approval.draft.pending_approval"


def test_the_release_13_message_has_a_tool_for_every_step_and_the_old_one_did_not():
    source, steps = eg.parse_next_steps(DRAFT_MSG)
    assert eg.missing_steps("Quotation", source, steps, NAMES) == []
    source, steps = eg.parse_next_steps(OLD_MSG)
    assert eg.missing_steps("Quotation", source, steps, NAMES) == ["cancelled (Cancel)", "expired (Expire)"]


@pytest.mark.parametrize("entity, status, tool", [
    ("Quotation", "draft", "Quotation.decline.sent.declined"),
    ("Deal", "new", "Deal.mark_lost.negotiation.closed_lost"),
    ("Deal", "negotiation", "Deal.mark_lost.new.closed_lost"),
    ("Lead", "qualified", "Lead.disqualify.new.disqualified"),
])
def test_the_probe_is_a_closing_edge_from_another_status(entity, status, tool):
    assert eg.probe_tool(entity, status, NAMES) == tool


class SweepPlatform:
    def __init__(self, moves=False, lands=False):
        self.moves, self.lands, self.calls = moves, lands, []
        self.rows = {"Quotation": [{"id": "q1", "status": "draft", "created_by": "me"},
                                   {"id": "q2", "status": "draft", "created_by": "me"},       # one per status
                                   {"id": "q3", "status": "sent", "created_by": "them"},      # not ours
                                   {"id": "q4", "status": "converted", "created_by": "me"}],  # terminal
                     "Deal": [], "Lead": []}

    def list_tools(self):
        return [{"name": n} for n in NAMES]

    def call(self, tool, args=None):
        self.calls.append((tool, (args or {}).get("id")))
        entity, op = tool.split(".", 1)
        if op == "list":
            return {"data": self.rows[entity]}
        if op == "get":
            return {"id": args["id"], "status": "sent" if self.moves else "draft"}
        if self.lands:
            return {}
        raise MCPToolError(DRAFT_MSG, code=INVALID_TRANSITION)


def test_sweep_probes_one_own_open_record_per_status():
    p = SweepPlatform()
    results = eg.sweep(p, "me")
    assert [(r["entity"], r["id"], r["result"]) for r in results] == [("Quotation", "q1", "pass")]
    assert results[0]["steps"] == ["pending_approval (Submit for Margin Approval)", "sent (Send)"]
    assert [c for c in p.calls if "decline" in c[0]] == [("Quotation.decline.sent.declined", "q1")]


def test_sweep_skips_a_record_that_moved_onto_the_probes_edge():
    p = SweepPlatform(moves=True)
    assert eg.sweep(p, "me")[0]["result"] == "skipped"
    assert not any("decline" in t for t, _ in p.calls)


def test_sweep_fails_a_probe_that_landed():
    assert eg.sweep(SweepPlatform(lands=True), "me")[0]["result"] == "fail"
