"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

The margin-approval route for an item this seat can't price, open since
Release 13 (N480, was drafts/BUG7): Quotation.create takes a line with no
rate, the draft is read back, then submitted (draft -> pending_approval) so
the approver prices it. The agent never sends a rate, never sends the quote
to the customer and never touches the deal. Also the harness side: the
write_quote_for_approval task and quotation_written's none_sent mode.
Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain import quotations
from domain.quotations import SUBMIT_TOOL, draft_for_approval, submit_for_approval
from harness import fixtures
from harness.verifiers import RunFiles, check_quotation_written, observations_for, validate_task

DEAL = {"id": "d1", "title": "T6-harness-x", "stage": "negotiation", "party_id": "p1",
        "company_id": "c1", "updated_at": "2026-10-22T10:00:00"}
PRICED = {"item_id": "i1", "item_name": "Lathe Dog", "requested_qty": 500,
          "outcome": "unpriced", "reason": "standard_rate redacted"}
# The live Quotation.create line after Release 13: only qty is required.
LIVE_LINE = {"type": "object", "properties": {"item_id": {}, "qty": {}, "rate": {}}, "required": ["qty"]}


class Platform:
    """Quotation create/get/submit as the platform behaves after Release 13."""

    def __init__(self, submit_listed=True, stored_qty=500, moves_to="pending_approval", on_deal=()):
        self.tools = [{"name": "Quotation.create",
                       "inputSchema": {"properties": {"items": {"type": "array", "items": LIVE_LINE}}}}]
        if submit_listed:
            self.tools.append({"name": SUBMIT_TOOL})
        self.stored_qty, self.moves_to = stored_qty, moves_to
        self.q, self.calls, self.args = None, [], {}
        # quotations already on the deal (and one on another deal: the
        # deal_id filter must be checked client-side)
        self.on_deal = [dict(q) for q in on_deal] + [
            {"id": "elsewhere", "number": "QTN-X", "deal_id": "d9", "status": "pending_approval",
             "items": [{"item_id": "i1", "qty": 500}]}]

    def list_tools(self):
        return self.tools

    def call(self, tool, args=None):
        self.calls.append(tool)
        self.args.setdefault(tool, []).append(args)
        if tool == "Deal.get":
            return dict(DEAL)
        if tool == "Quotation.list":
            return {"data": [dict(q) for q in self.on_deal]}
        if tool == "Quotation.create":
            line = args["items"][0]
            self.q = {"id": "q1", "number": "QTN-2026-00090", "status": "draft", "deal_id": args["deal_id"],
                      "items": [{"item_id": line["item_id"], "qty": self.stored_qty, "rate": None}]}
            return {"id": "q1"}
        if tool == "Quotation.get":
            return dict(self.q)
        if tool == SUBMIT_TOOL:
            self.q["status"] = self.moves_to
            return {}
        raise AssertionError(f"unexpected tool call {tool}")


def writes(p):
    return [t for t in p.calls if not t.endswith((".get", ".list"))]


def route(p, dry_run=False):
    """As the planner wires it: submit only after a draft (graph/planner.py)."""
    drafted = draft_for_approval(p, {"deal": dict(DEAL)}, PRICED, "r1", dry_run=dry_run)
    if drafted["outcome"] not in ("drafted", "would_create"):
        return drafted, drafted
    return drafted, submit_for_approval(p, drafted, dry_run=dry_run)


# --- the route ------------------------------------------------------------------

def test_live_schema_needs_no_rate():
    assert quotations.line_rate_required(Platform()) is False


def test_unpriced_quote_is_drafted_and_submitted_for_approval():
    p = Platform()
    drafted, out = route(p)
    assert drafted["outcome"] == "drafted" and drafted["readback_problems"] == []
    assert out["outcome"] == "pending_approval" and out["status"] == "pending_approval"
    assert writes(p) == ["Quotation.create", SUBMIT_TOOL]
    assert (out["quotation_id"], out["number"]) == ("q1", "QTN-2026-00090")


def test_no_rate_or_amount_is_ever_sent():
    p = Platform()
    route(p)
    line = p.args["Quotation.create"][0]["items"][0]
    assert "rate" not in line and "amount" not in line
    assert (line["item_id"], line["qty"]) == ("i1", 500)


def test_the_deal_is_reread_before_the_create_and_never_written():
    p = Platform()
    route(p)
    assert p.calls.index("Deal.get") < p.calls.index("Quotation.create")
    assert not any(t.startswith("Deal.") and t != "Deal.get" for t in p.calls)


def test_the_quote_is_never_sent_to_the_customer():
    # N480: an unpriced quote can't be sent; this agent never tries.
    p = Platform()
    route(p)
    assert not any(t.startswith(("Quotation.send", "Quotation.accept", "Quotation.convert",
                                 "Quotation.release_approved_quote")) for t in p.calls)


def test_the_platform_stored_rate_is_reported_as_the_platforms():
    drafted, _ = route(Platform())
    assert "platform_rate" in drafted and drafted["platform_rate"] is None


def test_dry_run_writes_nothing():
    p = Platform()
    drafted, out = route(p, dry_run=True)
    assert (drafted["outcome"], out["outcome"]) == ("would_create", "would_submit")
    assert writes(p) == []


def test_a_readback_mismatch_is_not_submitted():
    p = Platform(stored_qty=50)
    drafted, out = route(p)
    assert drafted["readback_problems"] == ["line qty: stored 50, sent 500"]
    assert out["outcome"] == "refused" and SUBMIT_TOOL not in p.calls


def test_no_submit_tool_leaves_the_draft():
    p = Platform(submit_listed=False)
    _, out = route(p)
    assert out["outcome"] == "refused" and "stays a draft" in out["reason"]
    assert writes(p) == ["Quotation.create"]


def test_a_submit_that_does_not_read_back_is_an_error():
    _, out = route(Platform(moves_to="draft"))
    assert out["outcome"] == "error" and "'draft'" in out["reason"]


# --- asked twice: no second quote for the same item -------------------------------

PENDING = {"id": "q0", "number": "QTN-2026-00088", "deal_id": "d1", "status": "pending_approval",
           "created_by": "u1", "items": [{"item_id": "i1", "qty": 500, "rate": None}]}


@pytest.mark.parametrize("status", quotations.IN_FLIGHT)
def test_an_in_flight_quote_for_the_item_blocks_a_second(status):
    p = Platform(on_deal=[{**PENDING, "status": status}])
    drafted, out = route(p)
    assert drafted["outcome"] == out["outcome"] == "refused"
    assert drafted["existing_quotation"]["number"] == "QTN-2026-00088"
    assert f"QTN-2026-00088 for this item is already {status}" in drafted["reason"]
    assert writes(p) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_duplicate_check_runs_after_the_reread_and_in_dry_runs(dry_run):
    p = Platform(on_deal=[PENDING])
    drafted, _ = route(p, dry_run=dry_run)
    assert drafted["outcome"] == "refused"
    assert p.calls[:2] == ["Deal.get", "Quotation.list"]


@pytest.mark.parametrize("other", [
    {**PENDING, "status": "declined"},                        # closed: may be quoted again
    {**PENDING, "status": "converted"},
    {**PENDING, "items": [{"item_id": "i2", "qty": 500}]},     # another item on the deal
])
def test_closed_quotes_and_other_items_do_not_block(other):
    drafted, out = route(Platform(on_deal=[other]))
    assert out["outcome"] == "pending_approval"


def test_another_deals_quote_does_not_block():
    # Platform always lists QTN-X on deal d9 with the same item.
    assert route(Platform())[1]["outcome"] == "pending_approval"


def test_the_refusal_reaches_the_answer():
    from llm.narrate import render_template
    from llm.verify import verify_claims
    drafted, out = route(Platform(on_deal=[PENDING]))
    finding = {"quotation": out, "refusals": []}
    text = render_template(finding)
    assert "QTN-2026-00088" in text and verify_claims(text, finding)["missing"] == []


# --- the harness: quotation_written(rate=none_sent) -------------------------------

FIXTURE = {"deal_id": "d1", "deal_title": "T6-harness-x", "item_id": "i1", "qty": 500, "me": "u1"}
OPEN = {"id": "d1", "stage": "negotiation", "updated_at": "t0"}
PARAMS = {"status": "pending_approval", "transition": "Quotation.submit_for_margin_approval",
          "rate": "none_sent"}
STORED = {"id": "q1", "number": "QTN-2026-00090", "deal_id": "d1", "status": "pending_approval",
          "created_by": "u1", "notes": "T6-harness quote", "net_total": 0, "total_tax": 0, "grand_total": 0,
          "items": [{"item_id": "i1", "qty": 500, "rate": None}]}
CALLS = [{"tool": "Deal.get", "args": {"id": "d1"}, "started_at": 1},
         {"tool": "Deal.get", "args": {"id": "d1"}, "started_at": 2},
         {"tool": "Quotation.create", "ok": True, "started_at": 3,
          "args": {"items": [{"item_id": "i1", "qty": 500}]}},
         {"tool": "Quotation.get", "started_at": 4},
         {"tool": SUBMIT_TOOL, "ok": True, "started_at": 5}]


def run(calls=CALLS, quotes=(STORED,), deal_after=OPEN, rate_required=False, section=None):
    before = {"quote_schema": {"present": True, "rate_required": rate_required},
              "deal:d1": {"row": OPEN}, "deal_quotations:d1": {"rows": []}}
    after = {"deal:d1": {"row": deal_after}, "deal_quotations:d1": {"rows": list(quotes)},
             "item_ref:i1": {"row": {"id": "i1"}}}
    sec = section or {"outcome": "pending_approval", "quotation_id": "q1", "number": "QTN-2026-00090"}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False, "finding": {"quotation": sec}},
                    calls=list(calls), before={"observations": before},
                    after={"observations": after}, fixture=FIXTURE)


def by_name(checks):
    return {c.name: c.status for c in checks}


@pytest.fixture(autouse=True)
def t6_prefix(monkeypatch):
    from harness import verifiers
    monkeypatch.setattr(verifiers, "T6_PREFIX", "T6-harness")


def test_verifier_fails_a_create_without_a_reread():
    got = by_name(check_quotation_written(run(calls=CALLS[1:]), PARAMS))
    assert got["reread_before_write"] == "fail"


def test_verifier_passes_a_clean_approval():
    got = by_name(check_quotation_written(run(), PARAMS))
    assert set(got.values()) == {"pass"}, got


def test_verifier_fails_a_rate_sent_by_the_agent():
    calls = [dict(c) for c in CALLS]
    calls[2]["args"] = {"items": [{"item_id": "i1", "qty": 500, "rate": 0}]}
    assert by_name(check_quotation_written(run(calls=calls), PARAMS))["no_rate_sent"] == "fail"


def test_verifier_fails_a_quote_left_draft_or_sent():
    for status in ("draft", "sent"):
        got = by_name(check_quotation_written(run(quotes=[{**STORED, "status": status}]), PARAMS))
        assert got["status"] == "fail", status


def test_verifier_fails_a_moved_deal_and_a_send():
    calls = CALLS + [{"tool": "Quotation.release_approved_quote", "ok": True, "started_at": 6}]
    got = by_name(check_quotation_written(run(calls=calls, deal_after={**OPEN, "stage": "closed_won"}), PARAMS))
    assert got["deal_untouched"] == got["no_bypass"] == "fail"


def test_verifier_errors_when_the_rate_is_required_again():
    got = by_name(check_quotation_written(run(rate_required=True), PARAMS))
    assert got == {"premise": "error"}


def test_priced_mode_has_no_premise_check():
    keys = observations_for({"verifiers": [{"name": "quotation_written", "params": {}}]})
    assert "quote_schema" not in keys


# --- the task -------------------------------------------------------------------

TASKS = json.loads((Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]


def test_the_approval_task_replaces_the_refusal():
    ids = {t["id"] for t in TASKS}
    assert "write_quote_for_approval" in ids and "refuse_unpriced_quote_rate_required" not in ids
    task = next(t for t in TASKS if t["id"] == "write_quote_for_approval")
    validate_task(task)
    assert task["requires_write"] and task["fixture"] == "open_deal_unpriced_item"
    assert task["expect"]["sections"]["quotation"] == "pending_approval"
    assert "quote_schema" in observations_for(task)


# --- clean-up: no approver is left pricing a test quote -----------------------------

class RetirePlatform:
    def __init__(self):
        self.quotes = [
            {"id": "ours", "deal_id": "d1", "status": "pending_approval", "created_by": "u1"},
            {"id": "theirs", "deal_id": "d1", "status": "pending_approval", "created_by": "u2"},
            {"id": "other_deal", "deal_id": "d9", "status": "pending_approval", "created_by": "u1"},
            {"id": "our_draft", "deal_id": "d1", "status": "draft", "created_by": "u1"}]
        self.calls = []

    def list_tools(self):
        return [{"name": "Deal.mark_lost.negotiation.closed_lost"}]

    def call(self, tool, args=None):
        self.calls.append((tool, (args or {}).get("id")))
        if tool == "Quotation.list":
            return {"data": [dict(q) for q in self.quotes], "total": len(self.quotes)}
        if tool == "Quotation.revise_quote":
            next(q for q in self.quotes if q["id"] == args["id"])["status"] = "draft"
            return {}
        if tool == "Quotation.get":
            return dict(next(q for q in self.quotes if q["id"] == args["id"]))
        if tool == "Deal.get":
            return {"id": "d1", "stage": "closed_lost" if ("Deal.mark_lost.negotiation.closed_lost", "d1")
                    in self.calls else "negotiation"}
        return {}


def test_retire_revises_only_our_pending_quote_on_the_fixture_deal():
    p = RetirePlatform()
    out = fixtures.retire(p, {"kind": "open_deal_unpriced_item", "deal_id": "d1", "me": "u1"})
    assert [r["quotation_id"] for r in out["revised"]] == ["ours"]
    assert out["revised"][0]["status_after"] == "draft"
    assert [i for t, i in p.calls if t == "Quotation.revise_quote"] == ["ours"]
    assert out["stage_after"] == "closed_lost"
