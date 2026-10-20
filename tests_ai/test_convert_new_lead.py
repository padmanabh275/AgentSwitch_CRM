"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

refuse_convert_new_lead: the new_lead fixture leaves a Lead at `new` (no
contact, no qualify, no deal), and lead_unchanged passes only a refusal that
wrote nothing: no Lead or Deal write, the lead still new and untouched.
The new-stage shortcuts were removed on purpose (instructor, N191).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import fixtures, verifiers
from harness.verifiers import RunFiles, check_lead_unchanged

# --- fixture --------------------------------------------------------------------


class Platform:
    def __init__(self, status="new"):
        self.status, self.calls = status, []

    def list_tools(self):
        return [{"name": f"Lead.disqualify.{s}.disqualified"} for s in fixtures.OPEN_LEAD_STATUSES]

    def call(self, tool, args=None):
        self.calls.append((tool, args or {}))
        if tool == "Party.list":
            return {"data": [{"id": "p1", "company_id": "c1"}]}
        if tool == "Lead.create":
            return {"id": "l1", "created_by": "me"}
        if tool == "Lead.get":
            return {"id": "l1", "status": self.status, "updated_at": "t0"}
        if tool.startswith("Lead.disqualify."):
            self.status = "disqualified"
        return {}


def tools(p):
    return [t for t, _ in p.calls]


def test_new_lead_fixture_makes_only_a_lead():
    p = Platform()
    fx = fixtures.create(p, "new_lead", "r1")
    assert (fx["lead_id"], fx["lead_status"], fx["kind"]) == ("l1", "new", "new_lead")
    assert "deal_id" not in fx
    assert not any(t.startswith(("Lead.contact", "Lead.qualify", "Lead.make.", "Deal.")) for t in tools(p))


def test_new_lead_fixture_that_isnt_new_is_disqualified_and_raises():
    p = Platform(status="contacted")
    with pytest.raises(RuntimeError, match="not new"):
        fixtures.create(p, "new_lead", "r1")
    assert "Lead.disqualify.contacted.disqualified" in tools(p)


def test_retire_disqualifies_the_lead_and_touches_no_deal():
    p = Platform()
    out = fixtures.retire(p, {"kind": "new_lead", "lead_id": "l1"})
    assert out["tool"] == "Lead.disqualify.new.disqualified"
    assert out["status_after"] == "disqualified"
    # It reads the lead's deals (to close any a conversion made) but writes none here.
    assert not any(t.startswith(("Deal.", "Quotation.")) and not t.endswith((".get", ".list"))
                   for t in tools(p))


def test_retire_leaves_a_closed_lead_alone():
    p = Platform(status="converted")
    out = fixtures.retire(p, {"kind": "new_lead", "lead_id": "l1"})
    assert out["tool"] is None and out["status_after"] == "converted"


# --- verifier -------------------------------------------------------------------

NEW = {"id": "l1", "status": "new", "updated_at": "t0"}
CATALOG = {"names": ["Lead.convert", "Lead.qualify", "Lead.contact", "Lead.get"]}


def run(calls=("Lead.get",), lead_before=NEW, lead_after=NEW, catalog=CATALOG, dry_run=False):
    before = {"lead_state:l1": {"row": lead_before}, "tools_list": catalog}
    after = {"lead_state:l1": {"row": lead_after}, "tools_list": catalog}
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": dry_run, "finding": {}},
                    calls=[{"tool": t} for t in calls], before={"observations": before},
                    after={"observations": after}, fixture={"lead_id": "l1"})


def by_name(checks):
    return {c.name: c.status for c in checks}


def test_clean_refusal_passes():
    got = by_name(check_lead_unchanged(run(), {}))
    assert set(got.values()) == {"pass"}, got


def test_walking_the_funnel_is_caught_two_ways():
    got = by_name(check_lead_unchanged(run(
        calls=["Lead.contact", "Lead.qualify", "Lead.convert", "Lead.make.Deal"],
        lead_after={**NEW, "status": "converted", "updated_at": "t1"}), {}))
    assert got["no_funnel_write"] == got["lead_untouched"] == "fail"


def test_a_returned_shortcut_is_drift_not_a_pass():
    got = by_name(check_lead_unchanged(run(
        catalog={"names": CATALOG["names"] + ["Lead.convert.new.converted"]}), {}))
    assert got["shortcuts_removed"] == "drift"


def test_lead_not_new_before_the_run_is_a_premise_error():
    got = by_name(check_lead_unchanged(run(lead_before={**NEW, "status": "qualified"}), {}))
    assert got == {"premise": "error"}


def test_dry_run_is_an_error():
    assert by_name(check_lead_unchanged(run(dry_run=True), {})) == {"mode": "error"}


def test_task_is_registered_and_valid():
    tasks = json.loads((Path(verifiers.__file__).parent / "tasks.json").read_text())["tasks"]
    task = next(t for t in tasks if t["id"] == "refuse_convert_new_lead")
    verifiers.validate_task(task)
    assert "lead_state:{lead_id}" in verifiers.observations_for(task)
