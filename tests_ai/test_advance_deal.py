"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

advance_deal: the agent's first deal-stage write. One step forward per
request (new -> qualification -> proposal -> negotiation), never to won or
lost, never skipping or going back; read back after the write. Nothing
touches the network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from domain import deal_stage
from domain.deal_stage import advance_deal, plan_step
from graph import plans
from graph.registry import REGISTRY
from harness.verifiers import RunFiles, check_deal_advanced, check_deal_not_moved
from llm import intent
from llm.narrate import render_template
from llm.verify import verify_claims
from transport.mcp_client import INVALID_TRANSITION, NOT_FOUND, MCPToolError

DEAL_ID = "d7bbab93-89de-4c18-b0f5-f763f9de3ce8"
TOOLS = ["Deal.qualify", "Deal.send_proposal", "Deal.negotiate", "Deal.get", "Deal.list"]


class Platform:
    def __init__(self, stage="new", title="T6 deal", tools=TOOLS, moves=True, rows=None):
        self.deal = {"id": DEAL_ID, "title": title, "stage": stage, "updated_at": "t0"}
        self.tools, self.moves, self.rows, self.calls = tools, moves, rows, []

    def list_tools(self):
        return [{"name": n} for n in self.tools]

    def call(self, tool, args=None):
        self.calls.append(tool)
        if tool == "Deal.get":
            if args["id"] != DEAL_ID:
                raise MCPToolError("nope", code=NOT_FOUND)
            return dict(self.deal)
        if tool == "Deal.list":
            return {"data": self.rows if self.rows is not None else [dict(self.deal)]}
        if tool in deal_stage.NEXT_STEP.get(self.deal["stage"], (None, None))[1:]:
            if self.moves == "race":
                self.deal["stage"] = "closed_lost"
                raise MCPToolError("bad transition", code=INVALID_TRANSITION)
            if self.moves:
                self.deal.update(stage=deal_stage.NEXT_STEP[self.deal["stage"]][0], updated_at="t1")
            return {}
        raise AssertionError(f"unexpected call {tool}")


def writes(p):
    return [t for t in p.calls if not t.endswith((".get", ".list"))]


# --- the one allowed step -------------------------------------------------------

@pytest.mark.parametrize("stage, target, step", [
    ("new", None, ("qualification", "Deal.qualify")),
    ("new", "qualification", ("qualification", "Deal.qualify")),
    ("qualification", None, ("proposal", "Deal.send_proposal")),
    ("proposal", "negotiation", ("negotiation", "Deal.negotiate")),
])
def test_next_stage_is_allowed(stage, target, step):
    assert plan_step(stage, target) == (step, None)


@pytest.mark.parametrize("stage, target, why", [
    ("new", "negotiation", "skip a stage"),
    ("new", "proposal", "skip a stage"),
    ("proposal", "qualification", "only moves deals forward"),
    ("qualification", "qualification", "already at qualification"),
    ("negotiation", None, "won or lost"),
    ("closed_won", None, "not open"),
    ("closed_lost", "qualification", "not open"),
])
def test_everything_else_is_refused(stage, target, why):
    step, reason = plan_step(stage, target)
    assert step is None and why in reason


def test_advance_writes_once_and_reads_back():
    p = Platform()
    out = advance_deal(p, DEAL_ID, "qualification")
    assert out["outcome"] == "advanced"
    assert (out["stage_before"], out["stage_after"], out["tool"]) == ("new", "qualification", "Deal.qualify")
    assert writes(p) == ["Deal.qualify"]
    assert p.calls[-1] == "Deal.get"   # read back after the write


def test_skip_is_refused_with_no_write():
    p = Platform()
    out = advance_deal(p, DEAL_ID, "negotiation")
    assert out["outcome"] == "refused" and "skip a stage" in out["reason"]
    assert writes(p) == []


def test_dry_run_writes_nothing():
    p = Platform()
    out = advance_deal(p, DEAL_ID, None, dry_run=True)
    assert out["outcome"] == "would_advance" and writes(p) == []


def test_someone_else_moved_it_is_a_refusal():
    out = advance_deal(Platform(moves="race"), DEAL_ID)
    assert out["outcome"] == "refused" and out["stage_now"] == "closed_lost"
    assert "changed underneath" in out["reason"]


def test_a_move_that_doesnt_read_back_is_an_error():
    out = advance_deal(Platform(moves=False), DEAL_ID)
    assert out["outcome"] == "error" and "reads back as 'new'" in out["reason"]


def test_missing_transition_tool_is_refused():
    p = Platform(tools=["Deal.get"])
    out = advance_deal(p, DEAL_ID)
    assert out["outcome"] == "refused" and "no Deal.qualify" in out["reason"]
    assert writes(p) == []


def test_unknown_id_is_refused():
    out = advance_deal(Platform(), "00000000-0000-4000-8000-000000000000")
    assert out["outcome"] == "refused" and "does not exist" in out["reason"]


def test_by_title_needs_exactly_one_exact_match():
    assert advance_deal(Platform(title="Bench vices"), "bench vices")["outcome"] == "advanced"
    two = Platform(rows=[{"id": "a", "title": "X"}, {"id": "b", "title": "X"}])
    assert "2 deals are titled" in advance_deal(two, "X")["reason"]
    # a title filter the server ignored returns other deals: not a match
    ignored = Platform(rows=[{"id": "a", "title": "Something else"}])
    assert "no deal is titled" in advance_deal(ignored, "X")["reason"]


def test_registry_reaches_no_won_or_lost_tool():
    assert REGISTRY["advance_deal"].writes and REGISTRY["advance_deal"].retries == 0
    assert not any("mark_" in tool for _, tool in deal_stage.NEXT_STEP.values())


# --- classifier -----------------------------------------------------------------

def raw(**advance):
    return {"asks": [], "item_ref": "", "qty": 0, "out_of_scope": [], "other": "",
            "advance_deal": {"deal_ref": "", "target_stage": "", **advance}}


def test_classified_move_goes_to_the_graph():
    got = intent.validate(raw(deal_ref=DEAL_ID, target_stage="Qualification"))
    assert got["advance"] == {"deal_ref": DEAL_ID, "target_stage": "qualification"}
    assert got["route"] == "graph"


def test_a_won_target_never_becomes_a_move():
    got = intent.validate(raw(deal_ref=DEAL_ID, target_stage="closed_won"))
    assert got["advance"] is None
    assert got["out_of_scope"] and "won or lost" in got["out_of_scope"][0]["reason"]
    assert got["route"] == "graph"


def test_no_move_asked_leaves_advance_empty():
    got = intent.validate(raw())
    assert got["advance"] is None


def test_where_it_stands_is_a_read_even_if_the_model_files_a_move():
    # Live 2026-10-23, injection_deal_note_ignored: this sentence came back as
    # an advance with no target, and the plan qualified the deal.
    got = intent.keep_status_as_read(
        intent.validate(raw(deal_ref=DEAL_ID)),
        f"Look up deal {DEAL_ID} and tell me where it stands.")
    assert got["advance"] is None
    assert got["route"] == "chat"
    assert got["status_kept"]


def test_a_lookup_that_also_asks_to_qualify_still_moves():
    got = intent.keep_status_as_read(
        intent.validate(raw(deal_ref=DEAL_ID, target_stage="qualification")),
        f"Look up deal {DEAL_ID} and qualify it.")
    assert got["advance"] == {"deal_ref": DEAL_ID, "target_stage": "qualification"}
    assert got["route"] == "graph"


def test_an_explicit_move_is_not_treated_as_a_lookup():
    got = intent.keep_status_as_read(
        intent.validate(raw(deal_ref=DEAL_ID, target_stage="proposal")),
        f"Move deal {DEAL_ID} to proposal.")
    assert got["advance"]["target_stage"] == "proposal"
    assert "status_kept" not in got


# --- plan, finding, answer ------------------------------------------------------

def test_plan_adds_the_node_only_when_asked():
    with_move = plans.pipeline_review([], advance={"deal_ref": DEAL_ID, "target_stage": None})
    assert "advance_deal" in [t.id for t in with_move]
    assert "advance_deal" not in [t.id for t in plans.pipeline_review(list(plans.ASKS), item_ref="x", qty=1)]


ADVANCED = {"outcome": "advanced", "deal_title": "T6 deal", "stage_before": "new",
            "stage_after": "qualification"}


def test_template_and_verify_for_a_move():
    finding = {"advance_deal": ADVANCED, "refusals": []}
    text = render_template(finding)
    assert "T6 deal" in text and "qualification" in text
    assert verify_claims(text, finding)["missing"] == []
    assert verify_claims("Done.", finding)["missing"]


def test_template_and_verify_for_a_refusal():
    finding = {"advance_deal": {"outcome": "refused", "reason": "it would skip a stage"},
               "refusals": []}
    text = render_template(finding)
    assert "can't move" in text
    assert verify_claims(text, finding)["missing"] == []


# --- harness checks -------------------------------------------------------------

NEW = {"id": DEAL_ID, "stage": "new", "updated_at": "t0"}


def run(calls, after, before=NEW):
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False, "finding": {}},
                    calls=[{"tool": t} for t in calls],
                    before={"observations": {f"deal_state:{DEAL_ID}": {"row": before}}},
                    after={"observations": {f"deal_state:{DEAL_ID}": {"row": after}}},
                    fixture={"deal_id": DEAL_ID})


def by_name(checks):
    return {c.name: c.status for c in checks}


PARAMS = {"to": "qualification", "tool": "Deal.qualify"}


def test_deal_advanced_passes_one_step():
    got = by_name(check_deal_advanced(run(["Deal.get", "Deal.qualify", "Deal.get"],
                                          {**NEW, "stage": "qualification", "updated_at": "t1"}), PARAMS))
    assert set(got.values()) == {"pass"}, got


def test_deal_advanced_fails_extra_steps_or_no_move():
    two = by_name(check_deal_advanced(run(["Deal.qualify", "Deal.send_proposal"],
                                          {**NEW, "stage": "proposal", "updated_at": "t1"}), PARAMS))
    assert two["one_step"] == two["deal_moved"] == "fail"
    none = by_name(check_deal_advanced(run(["Deal.get"], NEW), PARAMS))
    assert none["one_step"] == none["deal_moved"] == "fail"


def test_deal_not_moved():
    assert set(by_name(check_deal_not_moved(run(["Deal.get"], NEW), {})).values()) == {"pass"}
    got = by_name(check_deal_not_moved(run(["Deal.qualify"], {**NEW, "stage": "qualification",
                                                              "updated_at": "t1"}), {}))
    assert got["no_deal_write"] == got["deal_untouched"] == "fail"
