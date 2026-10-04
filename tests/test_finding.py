"""graph/finding.py: build_finding, driven by the args pipeline_review really builds."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from graph import plans
from graph.finding import build_finding
from graph.outcome import ANSWERED, ERROR, REFUSED, SKIPPED, NodeOutcome

CTX = SimpleNamespace(run_id="run-1", session_id="sess-1", dry_run=True)


def finding_args(asks=plans.ASKS, **kw):
    """The build_finding node's args, as the plan template produces them."""
    tasks = plans.pipeline_review(list(asks), **kw)
    return next(t for t in tasks if t.id == "build_finding").args


def answered(**data):
    return NodeOutcome(ANSWERED, data={"outcome": "answered", **data})


def test_every_section_present_even_when_not_asked():
    f = build_finding(finding_args(asks=["at_risk"]), {"at_risk": answered(deal_ids=["d1"])}, CTX)
    assert f["closing_this_month"] == {"outcome": "not_requested"}
    assert f["quote"] == {"outcome": "not_requested"}
    assert f["at_risk"]["deal_ids"] == ["d1"]
    assert f["refusals"] == [] and f["not_handled"] is None


def test_run_metadata():
    f = build_finding(finding_args(asks=["at_risk"]), {}, CTX)
    assert (f["run_id"], f["session_id"], f["dry_run"]) == ("run-1", "sess-1", True)
    assert dt.datetime.fromisoformat(f["generated_at"]).utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_quote_section_takes_the_furthest_node_that_ran():
    outcomes = {
        "resolve_item": answered(item_id="i1"),
        "price_lookup": NodeOutcome.from_domain({"outcome": "unpriced", "item_id": "i1"}),
        "escalate_quote": NodeOutcome.from_domain({"outcome": "escalated", "escalation_id": "e1"}),
    }
    f = build_finding(finding_args(asks=["quote"], item_ref="vice", qty=5), outcomes, CTX)
    assert f["quote"] == {"outcome": "escalated", "escalation_id": "e1"}


def test_quote_section_without_escalation_is_the_price():
    outcomes = {
        "resolve_item": answered(item_id="i1"),
        "price_lookup": NodeOutcome.from_domain({"outcome": "quoted", "total_price": 50.0}),
    }
    f = build_finding(finding_args(asks=["quote"], item_ref="vice", qty=5), outcomes, CTX)
    assert f["quote"]["total_price"] == 50.0


def test_quote_refused_at_resolution_skips_the_price():
    refusal = {"outcome": "refused", "reason": "no item matches 'lathe'"}
    outcomes = {
        "resolve_item": NodeOutcome.from_domain(refusal),
        "price_lookup": NodeOutcome(SKIPPED, reason="dependency resolve_item refused"),
    }
    f = build_finding(finding_args(asks=["quote"], item_ref="lathe", qty=5), outcomes, CTX)
    assert f["quote"] == refusal


def test_quote_with_no_quantity_is_refused_by_the_plan():
    args = finding_args(asks=["quote"], item_ref="vice", qty=None)
    outcomes = {"quote_refused": NodeOutcome(REFUSED, data={
        "outcome": "refused", "reason": "can't quote without knowing how many units"})}
    assert build_finding(args, outcomes, CTX)["quote"]["outcome"] == "refused"


def test_failed_snapshot_reports_the_error_not_just_skipped():
    outcomes = {
        "snapshot_deals": NodeOutcome(ERROR, reason="gateway timeout", error_code="transient"),
        "closing_this_month": NodeOutcome(SKIPPED, reason="dependency snapshot_deals failed"),
        "at_risk": NodeOutcome(SKIPPED, reason="dependency snapshot_deals failed"),
    }
    f = build_finding(finding_args(asks=["closing_this_month", "at_risk"]), outcomes, CTX)
    for name in ("closing_this_month", "at_risk"):
        assert f[name] == {"outcome": "error", "node": "snapshot_deals",
                           "error_code": "transient", "reason": "gateway timeout"}


def test_section_skipped_when_nothing_after_it_ran():
    outcomes = {"at_risk": NodeOutcome(SKIPPED, reason="budget exhausted")}
    f = build_finding(finding_args(asks=["at_risk"]), outcomes, CTX)
    assert f["at_risk"] == {"outcome": "skipped", "node": "at_risk", "reason": "budget exhausted"}


def test_section_with_no_outcome_at_all_is_an_error():
    f = build_finding(finding_args(asks=["at_risk"]), {}, CTX)
    assert f["at_risk"]["outcome"] == "error"
    assert "no node" in f["at_risk"]["reason"]


def test_refusals_and_not_handled():
    args = finding_args(asks=["at_risk"],
                        out_of_scope=[{"request": "email the client", "reason": "no send tool"},
                                      {"request": "edit payroll", "reason": "not this seat"}],
                        other="who owns deal 42?")
    outcomes = {
        "at_risk": answered(),
        "refuse_1": NodeOutcome(REFUSED, data={"outcome": "refused", "request": "email the client"}),
        "refuse_2": NodeOutcome(REFUSED, data={"outcome": "refused", "request": "edit payroll"}),
    }
    f = build_finding(args, outcomes, CTX)
    assert [r["request"] for r in f["refusals"]] == ["email the client", "edit payroll"]
    assert f["not_handled"] == {"request": "who owns deal 42?", "reason": plans.NOT_HANDLED_REASON}


def test_refusal_that_never_ran_is_left_out():
    args = finding_args(asks=["at_risk"], out_of_scope=[{"request": "x", "reason": "y"}])
    assert build_finding(args, {"at_risk": answered()}, CTX)["refusals"] == []


# --- NodeOutcome, which every section above is built from --------------------

@pytest.mark.parametrize("domain, status", [
    ("answered", "answered"), ("quoted", "answered"), ("unpriced", "answered"),
    ("refused", "refused"), ("escalated", "escalated"), ("error", "error"),
])
def test_from_domain_maps_outcomes(domain, status):
    out = NodeOutcome.from_domain({"outcome": domain, "reason": "r"})
    assert out.status == status and out.reason == "r"


def test_from_domain_rejects_unmapped_outcome():
    with pytest.raises(ValueError):
        NodeOutcome.from_domain({"outcome": "maybe"})


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        NodeOutcome("done")
