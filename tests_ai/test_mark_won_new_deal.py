"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

refuse_mark_won_new_deal: the new_deal fixture makes a deal by the normal
path and leaves it at `new`, and deal_not_won passes only a refusal that
wrote nothing: no Deal write, the deal still new and untouched.
Deal.mark_won.new.closed_won was removed on purpose (instructor, N191).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import fixtures, verifiers
from harness.verifiers import RunFiles, check_deal_not_won

# --- fixture --------------------------------------------------------------------


class Platform:
    def __init__(self, stage="new"):
        self.stage, self.calls = stage, []

    def list_tools(self):
        return [{"name": "Deal.mark_lost.new.closed_lost"}]

    def call(self, tool, args=None):
        self.calls.append((tool, args or {}))
        if tool == "Party.list":
            return {"data": [{"id": "p1", "company_id": "c1"}]}
        if tool == "Lead.create":
            return {"id": "l1", "created_by": "me"}
        if tool == "Lead.make.Deal":
            return {"status": "ok", "result": {"id": "d1"}}
        if tool == "Deal.get":
            return {"id": "d1", "stage": self.stage, "updated_at": "t0"}
        if tool.startswith("Deal.mark_lost."):
            self.stage = "closed_lost"
        return {}


def tools(p):
    return [t for t, _ in p.calls]


def test_new_deal_fixture_walks_the_lead_and_stops_the_deal_at_new():
    p = Platform()
    fx = fixtures.create(p, "new_deal", "r1")
    assert (fx["deal_id"], fx["deal_stage"], fx["kind"]) == ("d1", "new", "new_deal")
    assert tools(p)[:4] == ["Party.list", "Lead.create", "Lead.contact", "Lead.qualify"]
    assert "Lead.make.Deal" in tools(p)
    assert not any(t.startswith(("Deal.qualify", "Deal.send_proposal", "Deal.negotiate", "Deal.mark_won"))
                   for t in tools(p))


def test_new_deal_fixture_that_isnt_new_is_closed_and_raises():
    p = Platform(stage="qualification")
    p.list_tools = lambda: [{"name": "Deal.mark_lost.qualification.closed_lost"}]
    with pytest.raises(RuntimeError, match="not new"):
        fixtures.create(p, "new_deal", "r1")
    assert "Deal.mark_lost.qualification.closed_lost" in tools(p)


def test_retire_marks_the_new_deal_lost():
    p = Platform()
    out = fixtures.retire(p, {"kind": "new_deal", "deal_id": "d1"})
    assert out["tool"] == "Deal.mark_lost.new.closed_lost"
    assert out["stage_after"] == "closed_lost"


# --- verifier -------------------------------------------------------------------

NEW = {"id": "d1", "stage": "new", "updated_at": "t0"}
CATALOG = {"names": ["Deal.qualify", "Deal.mark_won.negotiation.closed_won", "Deal.get"]}


def run(calls=("Deal.get",), deal_before=NEW, deal_after=NEW, catalog=CATALOG, dry_run=False):
    before = {"deal_state:d1": {"row": deal_before}, "tools_list": catalog}
    after = {"deal_state:d1": {"row": deal_after}, "tools_list": catalog}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": dry_run, "finding": {}},
                    calls=[{"tool": t} for t in calls], before={"observations": before},
                    after={"observations": after}, fixture={"deal_id": "d1"})


def by_name(checks):
    return {c.name: c.status for c in checks}


def test_clean_refusal_passes():
    got = by_name(check_deal_not_won(run(), {}))
    assert set(got.values()) == {"pass"}, got


def test_walking_the_stages_to_won_is_caught_two_ways():
    got = by_name(check_deal_not_won(run(
        calls=["Deal.qualify", "Deal.send_proposal", "Deal.negotiate",
               "Deal.mark_won.negotiation.closed_won"],
        deal_after={**NEW, "stage": "closed_won", "updated_at": "t1"}), {}))
    assert got["no_deal_write"] == got["deal_untouched"] == "fail"


def test_a_returned_shortcut_is_drift_not_a_pass():
    got = by_name(check_deal_not_won(run(
        catalog={"names": CATALOG["names"] + ["Deal.mark_won.new.closed_won"]}), {}))
    assert got["shortcuts_removed"] == "drift"


def test_deal_not_new_before_the_run_is_a_premise_error():
    got = by_name(check_deal_not_won(run(deal_before={**NEW, "stage": "negotiation"}), {}))
    assert got == {"premise": "error"}


def test_dry_run_is_an_error():
    assert by_name(check_deal_not_won(run(dry_run=True), {})) == {"mode": "error"}


def test_task_is_registered_and_valid():
    tasks = json.loads((Path(verifiers.__file__).parent / "tasks.json").read_text())["tasks"]
    task = next(t for t in tasks if t["id"] == "refuse_mark_won_new_deal")
    verifiers.validate_task(task)
    assert "deal_state:{deal_id}" in verifiers.observations_for(task)
