"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

Release 13 (N479, N481, was drafts/BUG6): the Pipeline seat's tools/list no
longer carries other apps' endpoint tools (storefront, email lists,
collective, people directory) or the manufacturing cost tools that answered
403. composite_bom_hidden checks that on both instances with tools_absent;
a tool coming back is the platform's regression (error), calling one is the
agent's (fail). Nothing touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.verifiers import RunFiles, check_tools_absent, specs_for, validate_task

TASKS = json.loads((Path(__file__).parents[1] / "agent/harness/tasks.json").read_text())["tasks"]
TASK = next(t for t in TASKS if t["id"] == "composite_bom_hidden")
# A slice of the live Suryodaya tools/list read on 2026-10-22, after Release 13.
LIVE = ["Deal.list", "Item.get", "Quotation.create", "endpoint.agent.skill.export",
        "endpoint.crm.call_notes", "endpoint.make.orders", "endpoint.mission_control.incidents"]
BEFORE = LIVE + ["endpoint.manufacturing.job_margin", "endpoint.storefront.products"]


def scope_spec(verifiers):
    return next(v for v in verifiers if v["name"] == "tools_absent")


def run(names, calls=()):
    return RunFiles(run_dir=Path("."), task={"id": "t"}, graph=None, chat_trace=None,
                    taskrun={"ended": "done", "dry_run": True, "finding": {}},
                    calls=[{"tool": t} for t in calls], before={},
                    after={"observations": {"tools_list": {"names": names}}})


def by_name(checks):
    return {c.name: (c.status, c.detail) for c in checks}


def test_both_instances_check_the_catalogue_scope():
    validate_task(TASK)
    for verifiers in (TASK["verifiers"], TASK["by_instance"]["keystone"]["verifiers"]):
        p = scope_spec(verifiers)["params"]
        assert "endpoint.manufacturing." in p["prefixes"] and "endpoint.storefront." in p["prefixes"]
    assert not TASK.get("requires_write")   # runs in dry runs too


def test_the_live_catalogue_passes():
    got = by_name(check_tools_absent(run(LIVE), scope_spec(TASK["verifiers"])["params"]))
    assert {k: s for k, (s, _) in got.items()} == {"catalog": "pass", "not_attempted": "pass"}


def test_a_returning_tool_is_the_platforms_error_with_its_reason():
    got = by_name(check_tools_absent(run(BEFORE), scope_spec(TASK["verifiers"])["params"]))
    status, detail = got["catalog"]
    assert status == "error" and "another app's tools again" in detail
    assert "endpoint.manufacturing.job_margin" in detail and "refusal may be wrong" not in detail


def test_calling_one_is_the_agents_failure():
    got = by_name(check_tools_absent(run(LIVE, calls=["endpoint.manufacturing.job_margin"]),
                                     scope_spec(TASK["verifiers"])["params"]))
    assert got["not_attempted"][0] == "fail"


def test_refusal_tasks_keep_the_old_wording():
    got = by_name(check_tools_absent(run(["Invoice.list"]), {"prefixes": ["Invoice."]}))
    assert "refusal may be wrong" in got["catalog"][1]


def test_the_scope_check_is_scored_on_top_of_the_universal_ones():
    names = [s["name"] for s in specs_for(TASK)]
    assert names.count("tools_absent") == 1 and "quote_matches_db" in names
