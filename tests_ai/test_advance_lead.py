"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

advance_lead: one step forward per request (new -> contacted -> qualified ->
converted), never skipping or going back, never disqualifying. Converting
uses Lead.convert, which makes the deal itself (probed live 2026-10-21). A
lead that already has a deal is converted too: since Release 13 (N482)
Lead.convert links that deal instead of making a second one (drafts/BUG8),
and a second deal after the convert is an error. Nothing touches the network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from domain import lead_stage
from domain.lead_stage import advance_lead, plan_step
from graph import plans
from graph.registry import REGISTRY
from harness import fixtures
from harness.verifiers import RunFiles, check_expectations, check_lead_advanced
from llm import intent
from llm.narrate import render_template
from llm.verify import verify_claims
from transport.mcp_client import INVALID_TRANSITION, NOT_FOUND, MCPToolError

LEAD = "38432a3c-753d-4f76-8e88-bc11ab42afad"
TOOLS = ["Lead.contact", "Lead.qualify", "Lead.convert", "Lead.get", "Deal.list"]


class Platform:
    # links: Lead.convert links a deal the lead already has (Release 13, N482);
    # links=False is the platform before the fix, which made a second one (BUG8).
    def __init__(self, status="new", deals=(), tools=TOOLS, moves=True, extra_deal=False, links=True):
        self.lead = {"id": LEAD, "status": status, "party_id": "p1", "updated_at": "t0"}
        self.deals = [dict(d) for d in deals]
        self.tools, self.moves, self.extra_deal, self.links, self.calls = tools, moves, extra_deal, links, []

    def list_tools(self):
        return [{"name": n} for n in self.tools]

    def call(self, tool, args=None):
        self.calls.append(tool)
        if tool == "Lead.get":
            if args["id"] != LEAD:
                raise MCPToolError("nope", code=NOT_FOUND)
            return dict(self.lead)
        if tool == "Deal.list":
            # one row from another lead: the filter must be checked client-side
            return {"data": self.deals + [{"id": "x", "title": "other", "stage": "new", "lead_id": "zz"}]}
        if tool == lead_stage.NEXT_STEP.get(self.lead["status"], (None, None))[1]:
            if self.moves == "race":
                self.lead["status"] = "disqualified"
                raise MCPToolError("bad transition", code=INVALID_TRANSITION)
            if self.moves:
                self.lead.update(status=lead_stage.NEXT_STEP[self.lead["status"]][0], updated_at="t1")
                if tool == "Lead.convert" and not (self.links and self.deals):
                    self.deals.append({"id": "d1", "title": "Deal from H4 party", "stage": "new",
                                       "lead_id": LEAD})
                    if self.extra_deal:
                        self.deals.append({"id": "d2", "title": "dup", "stage": "new", "lead_id": LEAD})
            return {}
        raise AssertionError(f"unexpected call {tool}")


def writes(p):
    return [t for t in p.calls if not t.endswith((".get", ".list"))]


# --- the one allowed step -------------------------------------------------------

@pytest.mark.parametrize("status, target, step", [
    ("new", None, ("contacted", "Lead.contact")),
    ("new", "contacted", ("contacted", "Lead.contact")),
    ("contacted", None, ("qualified", "Lead.qualify")),
    ("qualified", "converted", ("converted", "Lead.convert")),
])
def test_next_step_is_allowed(status, target, step):
    assert plan_step(status, target) == (step, None)


@pytest.mark.parametrize("status, target, why", [
    ("new", "converted", "skip a step"),
    ("new", "qualified", "skip a step"),
    ("qualified", "contacted", "only moves leads forward"),
    ("contacted", "contacted", "already contacted"),
    ("converted", None, "nothing to move"),
    ("disqualified", "contacted", "nothing to move"),
])
def test_everything_else_is_refused(status, target, why):
    step, reason = plan_step(status, target)
    assert step is None and why in reason


def test_contact_writes_once_and_reads_back():
    p = Platform()
    out = advance_lead(p, LEAD, "contacted")
    assert (out["outcome"], out["status_before"], out["status_after"]) == ("advanced", "new", "contacted")
    assert writes(p) == ["Lead.contact"] and p.calls[-1] == "Lead.get"
    assert "deal_id" not in out


def test_convert_uses_lead_convert_and_names_the_deal_it_made():
    p = Platform(status="qualified")
    out = advance_lead(p, LEAD, "converted")
    assert out["outcome"] == "advanced" and writes(p) == ["Lead.convert"]
    assert (out["deal_id"], out["deal_title"]) == ("d1", "Deal from H4 party")
    assert "Lead.make.Deal" not in p.calls


T6_DEAL = {"id": "d0", "title": "T6 deal", "stage": "new", "lead_id": LEAD}


def test_a_lead_that_already_has_a_deal_is_converted_and_keeps_it():
    # Lead.make.Deal leaves the lead qualified; since N482 converting links that deal.
    p = Platform(status="qualified", deals=[T6_DEAL])
    out = advance_lead(p, LEAD)
    assert out["outcome"] == "advanced" and writes(p) == ["Lead.convert"]
    assert (out["deal_id"], out["deal_linked"]) == ("d0", True)
    assert "linked the deal it already had, 'T6 deal'" in out["reason"]
    assert out["existing_deals"] == [T6_DEAL]


def test_a_new_deal_is_reported_as_created_not_linked():
    out = advance_lead(Platform(status="qualified"), LEAD)
    assert out["deal_linked"] is False and "created deal" in out["reason"]


def test_the_old_second_deal_behaviour_is_an_error_not_success():
    # If BUG8 came back, the convert lands but the lead ends with two deals.
    p = Platform(status="qualified", deals=[T6_DEAL], links=False)
    out = advance_lead(p, LEAD)
    assert out["outcome"] == "error" and "2 deal(s)" in out["reason"]


def test_a_convert_that_swaps_the_deal_is_an_error():
    class Swaps(Platform):
        def call(self, tool, args=None):
            out = super().call(tool, args)
            if tool == "Lead.convert":
                self.deals = [{"id": "d9", "title": "other", "stage": "new", "lead_id": LEAD}]
            return out
    out = advance_lead(Swaps(status="qualified", deals=[T6_DEAL]), LEAD)
    assert out["outcome"] == "error" and "not the deal it had ('T6 deal')" in out["reason"]


def test_a_lead_with_two_deals_is_not_converted():
    p = Platform(status="qualified", deals=[T6_DEAL, {**T6_DEAL, "id": "d00", "title": "T6 deal 2"}])
    out = advance_lead(p, LEAD)
    assert out["outcome"] == "refused" and "already has 2 deals" in out["reason"]
    assert writes(p) == []


def test_two_deals_after_convert_is_an_error():
    out = advance_lead(Platform(status="qualified", extra_deal=True), LEAD)
    assert out["outcome"] == "error" and "2 deal(s)" in out["reason"]


def test_skip_and_closed_leads_write_nothing():
    for status, target in (("new", "converted"), ("converted", None)):
        p = Platform(status=status)
        assert advance_lead(p, LEAD, target)["outcome"] == "refused"
        assert writes(p) == []


def test_dry_run_race_readback_missing_tool_and_bad_ref():
    assert advance_lead(Platform(), LEAD, dry_run=True)["outcome"] == "would_advance"
    race = advance_lead(Platform(moves="race"), LEAD)
    assert race["outcome"] == "refused" and "changed underneath" in race["reason"]
    assert advance_lead(Platform(moves=False), LEAD)["outcome"] == "error"
    assert "no Lead.contact" in advance_lead(Platform(tools=["Lead.get"]), LEAD)["reason"]
    assert "does not exist" in advance_lead(Platform(), "00000000-0000-4000-8000-000000000000")["reason"]
    assert "give the lead id" in advance_lead(Platform(), "Acme lead")["reason"]


def test_registry_entry_is_a_never_retried_write():
    assert REGISTRY["advance_lead"].writes and REGISTRY["advance_lead"].retries == 0
    assert all(tool != "Lead.make.Deal" and "disqualify" not in tool
               for _, tool in lead_stage.NEXT_STEP.values())


# --- classifier -----------------------------------------------------------------

def raw(**lead):
    return {"asks": [], "item_ref": "", "qty": 0, "out_of_scope": [], "other": "",
            "advance_deal": {"deal_ref": "", "target_stage": ""},
            "advance_lead": {"lead_ref": "", "target_status": "", **lead}}


def test_classified_lead_move_goes_to_the_graph():
    got = intent.validate(raw(lead_ref=LEAD, target_status="Converted"))
    assert got["advance_lead"] == {"lead_ref": LEAD, "target_status": "converted"}
    assert got["route"] == "graph"


def test_disqualify_never_becomes_a_move():
    got = intent.validate(raw(lead_ref=LEAD, target_status="disqualified"))
    assert got["advance_lead"] is None and "can't disqualify" in got["out_of_scope"][0]["reason"]


@pytest.mark.parametrize("said, means", [
    ("deal", "converted"), ("Deal", "converted"), ("convert", "converted"),
    ("converted to deal", "converted"), ("contact", "contacted"), ("qualify", "qualified"),
])
def test_what_models_write_for_a_step_is_mapped(said, means):
    # Live 2026-10-21: "convert it into a deal" came back as target_status "deal".
    got = intent.validate(raw(lead_ref=LEAD, target_status=said))
    assert got["advance_lead"]["target_status"] == means and not got["out_of_scope"]


def test_an_unknown_step_is_refused_with_the_right_reason():
    got = intent.validate(raw(lead_ref=LEAD, target_status="won"))
    assert got["advance_lead"] is None
    reason = got["out_of_scope"][0]["reason"]
    assert "isn't a lead step" in reason and "disqualify" not in reason


def test_a_skip_passes_through_for_the_domain_to_refuse():
    got = intent.validate(raw(lead_ref=LEAD, target_status="converted"))
    assert got["advance_lead"]["target_status"] == "converted" and not got["out_of_scope"]


# --- plan, answer ---------------------------------------------------------------

def test_plan_adds_the_node_only_when_asked():
    tasks = plans.pipeline_review([], advance_lead={"lead_ref": LEAD, "target_status": None})
    assert "advance_lead" in [t.id for t in tasks]
    assert "advance_lead" not in [t.id for t in plans.pipeline_review([], advance={"deal_ref": "x"})]


def test_template_and_verify_for_a_conversion():
    finding = {"advance_lead": {"outcome": "advanced", "status_before": "qualified",
                                "status_after": "converted", "deal_title": "Deal from H4 party"},
               "refusals": []}
    text = render_template(finding)
    assert "converted" in text and "Deal from H4 party" in text
    assert verify_claims(text, finding)["missing"] == []
    assert verify_claims("Lead converted.", finding)["missing"]   # the deal must be named


def test_template_and_verify_for_a_refusal():
    finding = {"advance_lead": {"outcome": "refused", "reason": "it would skip a step"}, "refusals": []}
    text = render_template(finding)
    assert "can't move" in text and verify_claims(text, finding)["missing"] == []


# --- fixture and harness checks -------------------------------------------------

class FixturePlatform:
    def __init__(self):
        self.calls, self.status, self.deals = [], "new", []

    def list_tools(self):
        return [{"name": n} for n in ("Lead.disqualify.qualified.disqualified",
                                      "Deal.mark_lost.new.closed_lost")]

    def call(self, tool, args=None):
        self.calls.append(tool)
        if tool == "Party.list":
            return {"data": [{"id": "p1", "company_id": "c1"}]}
        if tool == "Lead.create":
            return {"id": "l1", "created_by": "me"}
        if tool == "Lead.contact":
            self.status = "contacted"
        if tool == "Lead.qualify":
            self.status = "qualified"
        if tool == "Lead.get":
            return {"id": "l1", "status": self.status, "updated_at": "t0"}
        if tool == "Deal.list":
            return {"data": self.deals}
        if tool == "Deal.get":
            return {"id": "d1", "stage": self.deals[0]["stage"] if self.deals else "new"}
        if tool.startswith("Deal.mark_lost."):
            self.deals[0]["stage"] = "closed_lost"
        return {}


def test_qualified_lead_fixture_walks_the_lead_and_makes_no_deal():
    p = FixturePlatform()
    fx = fixtures.create(p, "qualified_lead", "r1")
    assert (fx["kind"], fx["lead_status"]) == ("qualified_lead", "qualified")
    assert "Lead.make.Deal" not in p.calls and "Lead.convert" not in p.calls


def test_retire_closes_the_deal_a_conversion_made():
    p = FixturePlatform()
    p.status = "converted"
    p.deals = [{"id": "d1", "lead_id": "l1", "stage": "new"}]
    out = fixtures.retire(p, {"kind": "qualified_lead", "lead_id": "l1"})
    assert out["closed_deals"][0]["stage_after"] == "closed_lost"
    assert out["tool"] is None   # a converted lead is closed already: no disqualify


def run(calls, after, deals_before=(), deals_after=(), before=None, finding=None):
    before = before or {"id": LEAD, "status": "qualified", "updated_at": "t0"}
    obs = lambda row, deals: {f"lead_state:{LEAD}": {"row": row}, f"lead_deals:{LEAD}": {"rows": list(deals)}}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": False, "finding": finding or {}},
                    calls=[{"tool": t} for t in calls], before={"observations": obs(before, deals_before)},
                    after={"observations": obs(after, deals_after)}, fixture={"lead_id": LEAD})


CONVERT = {"from": "qualified", "to": "converted", "tool": "Lead.convert", "makes_deal": True}
CONVERTED = {"id": LEAD, "status": "converted", "updated_at": "t1"}


def by_name(checks):
    return {c.name: c.status for c in checks}


def test_lead_advanced_passes_one_convert_with_one_deal():
    got = by_name(check_lead_advanced(run(["Lead.get", "Lead.convert", "Lead.get"], CONVERTED,
                                          deals_after=[{"id": "d1"}]), CONVERT))
    assert set(got.values()) == {"pass"}, got


def test_make_deal_plus_convert_is_caught():
    got = by_name(check_lead_advanced(run(["Lead.make.Deal", "Lead.convert"], CONVERTED,
                                          deals_after=[{"id": "d1"}, {"id": "d2"}]), CONVERT))
    assert got["one_step"] == got["one_deal"] == "fail"


def test_an_action_section_refusal_counts_toward_refusals_min():
    f = RunFiles(run_dir=Path("."), task={"id": "t", "expect": {"refusals_min": 1}}, graph=None,
                 chat_trace=None, calls=[], before={}, after={},
                 taskrun={"ended": "done", "finding": {"refusals": [],
                                                       "advance_lead": {"outcome": "refused"}}})
    assert by_name(check_expectations(f, {}))["refusals"] == "pass"


# --- N482 (was BUG8): a qualified lead that already has a deal ------------------

class WithDealPlatform(FixturePlatform):
    def call(self, tool, args=None):
        if tool == "Lead.make.Deal":
            self.calls.append(tool)
            self.deals.append({"id": "d0", "lead_id": "l1", "stage": "new"})
            return {"status": "ok", "result": {"id": "d0"}}
        return super().call(tool, args)


def test_with_deal_fixture_makes_one_deal_and_leaves_the_lead_qualified():
    p = WithDealPlatform()
    fx = fixtures.create(p, "qualified_lead_with_deal", "r1")
    assert (fx["kind"], fx["lead_status"], fx["deal_id"]) == ("qualified_lead_with_deal", "qualified", "d0")
    assert "Lead.convert" not in p.calls


def test_with_deal_retire_closes_the_deal_and_disqualifies_the_lead():
    p = WithDealPlatform()
    p.status, p.deals = "qualified", [{"id": "d0", "lead_id": "l1", "stage": "new"}]
    out = fixtures.retire(p, {"kind": "qualified_lead_with_deal", "lead_id": "l1"})
    assert out["closed_deals"][0]["stage_after"] == "closed_lost"
    assert out["tool"] == "Lead.disqualify.qualified.disqualified"


KEEP = {"from": "qualified", "to": "converted", "tool": "Lead.convert", "keeps_deal": True}
LINKED = {"advance_lead": {"outcome": "advanced", "deal_id": "d0", "deal_linked": True}}


def test_keeps_deal_passes_a_convert_that_links_the_deal():
    got = by_name(check_lead_advanced(run(["Lead.get", "Deal.list", "Lead.convert", "Lead.get"], CONVERTED,
                                          deals_before=[{"id": "d0"}], deals_after=[{"id": "d0"}],
                                          finding=LINKED), KEEP))
    assert set(got.values()) == {"pass"}, got


def test_keeps_deal_catches_the_second_deal():
    got = by_name(check_lead_advanced(run(["Lead.convert"], CONVERTED, deals_before=[{"id": "d0"}],
                                          deals_after=[{"id": "d0"}, {"id": "d1"}], finding=LINKED), KEEP))
    assert got["deal_linked"] == got["finding_matches"] == "fail"


def test_keeps_deal_catches_a_finding_that_says_created():
    finding = {"advance_lead": {"outcome": "advanced", "deal_id": "d0", "deal_linked": False}}
    got = by_name(check_lead_advanced(run(["Lead.convert"], CONVERTED, deals_before=[{"id": "d0"}],
                                          deals_after=[{"id": "d0"}], finding=finding), KEEP))
    assert (got["deal_linked"], got["finding_matches"]) == ("pass", "fail")


def test_keeps_deal_needs_exactly_one_deal_before():
    got = by_name(check_lead_advanced(run(["Lead.convert"], CONVERTED, deals_after=[{"id": "d1"}]), KEEP))
    assert got == {"premise": "error"}


def test_the_with_deal_task_converts_and_checks_the_deal_is_kept():
    import json
    tasks = json.loads((Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]
    ids = {t["id"] for t in tasks}
    task = next(t for t in tasks if t["id"] == "convert_lead_with_deal")
    assert "refuse_convert_lead_with_deal" not in ids
    assert task["fixture"] == "qualified_lead_with_deal" and task["requires_write"]
    assert task["verifiers"][0]["params"]["keeps_deal"] is True
