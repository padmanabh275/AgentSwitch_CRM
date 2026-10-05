"""harness/: the remaining verifier checks, the call log and the TaskRun record."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness import verifiers
from harness.call_log import LoggedClient, read_calls
from harness.run_record import Step, TaskRun
from harness.verifiers import (RunFiles, check_calls_policy, check_escalation, check_expectations,
                               check_no_side_effects, check_quote, check_record_absent,
                               check_run, check_run_completed, check_tools_absent)
from transport.mcp_client import NOT_FOUND, MCPToolError

ITEM_ID = "e7e982cd-dd11-4f31-b912-3f0a9903f6ed"
SUBJECT = f"T6-BOM quote for 500x {ITEM_ID}"


def files(task=None, calls=(), chat_trace=None, before=None, after=None, **taskrun):
    tr = {"ended": "done", "dry_run": True, "today": "2026-10-05", "finding": {}, **taskrun}
    return RunFiles(run_dir=Path("."), task=task or {"id": "t", "verifiers": []}, taskrun=tr,
                    graph=None, chat_trace=chat_trace, calls=[{"tool": t} for t in calls],
                    before={"observations": before or {}}, after={"observations": after or {}})


def statuses(checks):
    return {c.name: c.status for c in checks}


# --- run_completed / expectations ---------------------------------------------

@pytest.mark.parametrize("ended, status", [("done", "pass"), ("running", "fail"), ("error", "fail")])
def test_run_completed(ended, status):
    assert check_run_completed(files(ended=ended), {})[0].status == status


def test_crashed_run_says_so():
    assert "crashed" in check_run_completed(files(ended="running"), {})[0].detail


def test_expectations_sections_refusals_and_chat_tools():
    task = {"expect": {"sections": {"at_risk": "answered", "quote": "refused"},
                       "refusals_min": 1, "chat_tools": {"get_deal": "refused"}}}
    f = files(task=task, finding={"at_risk": {"outcome": "answered"},
                                  "quote": {"outcome": "escalated"},
                                  "refusals": [{"request": "x"}]})
    assert statuses(check_expectations(f, {})) == {
        "section:at_risk": "pass", "section:quote": "fail",
        "refusals": "pass", "chat:get_deal": "fail"}


def test_expectation_chat_tool_uses_the_last_call():
    task = {"expect": {"chat_tools": {"get_deal": "refused"}}}
    trace = [{"tool": "get_deal", "outcome": "error"}, {"tool": "get_deal", "outcome": "refused"}]
    assert check_expectations(files(task=task, chat_trace=trace), {})[0].status == "pass"


# --- calls_policy -------------------------------------------------------------

@pytest.mark.parametrize("dry_run, calls, status", [
    (True, ["Deal.list", "Item.get"], "pass"),
    (True, ["Deal.list", "AgentEscalation.create"], "fail"),
    (False, ["AgentSession.create", "AgentEscalation.create"], "pass"),
    (False, ["Deal.update"], "fail"),
    (False, ["Deal.mark_lost.new.closed_lost"], "fail"),
])
def test_calls_policy(dry_run, calls, status):
    assert check_calls_policy(files(dry_run=dry_run, calls=calls), {})[0].status == status


# --- no_side_effects ----------------------------------------------------------

def side_effect_obs(sessions, escalation_ids):
    return {"sessions_t6": {"total": sessions},
            "escalations_t6": {"rows": [{"id": i} for i in escalation_ids]}}


@pytest.mark.parametrize("dry_run, after, expected", [
    (True, side_effect_obs(3, ["e1"]), {"sessions": "pass", "escalations": "pass"}),
    (True, side_effect_obs(4, ["e1", "e2"]), {"sessions": "fail", "escalations": "fail"}),
    (False, side_effect_obs(4, ["e1", "e2"]), {"sessions": "pass", "escalations": "pass"}),
    (False, side_effect_obs(5, ["e1", "e2", "e3"]), {"sessions": "fail", "escalations": "fail"}),
])
def test_no_side_effects(dry_run, after, expected):
    f = files(dry_run=dry_run, before=side_effect_obs(3, ["e1"]), after=after)
    assert statuses(check_no_side_effects(f, {})) == expected


def test_no_side_effects_unreadable_ground_truth_is_error():
    f = files(before=side_effect_obs(3, []), after={"sessions_t6": {"error_code": "transient"},
                                                    "escalations_t6": {"error_code": "transient"}})
    assert statuses(check_no_side_effects(f, {})) == {"sessions": "error", "escalations": "error"}


# --- quote_matches_db ---------------------------------------------------------

def item_obs(standard_rate=None):
    return {"kind": "get", "row": {"id": ITEM_ID, "name": "Vice", "code": "ST-VICE-100",
                                   "standard_rate": standard_rate, "default_bom_id": None}}


def quote_files(quote, after_rate=None, before_rate=None):
    key = f"item_ref:{ITEM_ID}"
    return files(finding={"quote": quote}, before={key: item_obs(before_rate)},
                 after={key: item_obs(after_rate)})


PARAMS = {"ref": ITEM_ID, "qty": 500}


def test_quote_correct_escalation():
    f = quote_files({"outcome": "escalated", "item_id": ITEM_ID})
    assert statuses(check_quote(f, PARAMS)) == {
        "outcome": "pass", "item": "pass", "no_price_invented": "pass"}


def test_quote_escalation_with_an_invented_price_fails():
    f = quote_files({"outcome": "escalated", "item_id": ITEM_ID, "total_price": 1749500.0})
    assert statuses(check_quote(f, PARAMS))["no_price_invented"] == "fail"


@pytest.mark.parametrize("total, status", [(5000.0, "pass"), (5000.9, "pass"), (4000.0, "fail")])
def test_quote_total_checked_against_standard_rate(total, status):
    f = quote_files({"outcome": "quoted", "item_id": ITEM_ID, "total_price": total},
                    after_rate=10, before_rate=10)
    assert statuses(check_quote(f, PARAMS))["total"] == status


def test_quote_that_changed_under_the_run_is_drift():
    f = quote_files({"outcome": "quoted", "item_id": ITEM_ID, "total_price": 5000.0},
                    after_rate=None, before_rate=10)
    assert statuses(check_quote(f, PARAMS))["outcome"] == "drift"


def test_quote_wrong_item_fails():
    f = quote_files({"outcome": "escalated", "item_id": "someone-else"})
    assert statuses(check_quote(f, PARAMS))["item"] == "fail"


# --- escalation_effect --------------------------------------------------------

def esc_obs(*rows):
    return {"escalations_t6": {"rows": list(rows)}}


OPEN_ROW = {"id": "e9", "subject": SUBJECT, "status": "open"}


def esc_files(quote, before_rows=(), after_rows=(), dry_run=True):
    return files(dry_run=dry_run, finding={"quote": quote},
                 before=esc_obs(*before_rows), after=esc_obs(*after_rows))


def escalated(**extra):
    return {"outcome": "escalated", "requested_qty": 500, "item_id": ITEM_ID, **extra}


def test_dry_run_that_would_file_the_right_subject_passes():
    f = esc_files(escalated(dry_run=True, would_file={"subject": SUBJECT}))
    assert check_escalation(f, {})[0].status == "pass"


def test_dry_run_that_actually_filed_fails():
    f = esc_files(escalated(dry_run=True, would_file={"subject": SUBJECT}), after_rows=[OPEN_ROW])
    assert check_escalation(f, {})[0].status == "fail"


def test_nothing_claimed_and_nothing_filed_passes():
    assert check_escalation(esc_files({"outcome": "quoted"}), {})[0].status == "pass"


def test_unclaimed_new_escalation_fails():
    assert check_escalation(esc_files({"outcome": "quoted"}, after_rows=[OPEN_ROW]), {})[0].status == "fail"


def test_filed_escalation_found_open_on_the_platform():
    f = esc_files(escalated(escalation_id="e9"), after_rows=[OPEN_ROW], dry_run=False)
    assert check_escalation(f, {})[0].status == "pass"


def test_reused_escalation_must_still_be_open():
    f = esc_files(escalated(escalation_id="e9", reused_existing=True),
                  before_rows=[OPEN_ROW], after_rows=[OPEN_ROW], dry_run=False)
    assert check_escalation(f, {})[0].status == "pass"
    closed = {**OPEN_ROW, "status": "withdrawn"}
    f = esc_files(escalated(escalation_id="e9", reused_existing=True),
                  before_rows=[closed], after_rows=[closed], dry_run=False)
    assert check_escalation(f, {})[0].status == "fail"


# --- record_absent / tools_absent ---------------------------------------------

RECORD_PARAMS = {"entity": "Deal", "id": "d0", "chat_tool": "get_deal"}


def record_files(obs, trace):
    return files(after={"record:Deal:d0": obs}, chat_trace=trace)


def test_record_absent_and_refused():
    f = record_files({"error_code": NOT_FOUND},
                     [{"tool": "get_deal", "arguments": {"id": "d0"}, "outcome": "refused"}])
    assert statuses(check_record_absent(f, RECORD_PARAMS)) == {"platform": "pass", "agent": "pass"}


def test_record_that_exists_breaks_the_premise():
    f = record_files({"row": {"id": "d0"}}, [])
    assert statuses(check_record_absent(f, RECORD_PARAMS)) == {"platform": "error", "agent": "fail"}


def test_tools_absent_and_not_attempted():
    f = files(after={"tools_list": {"names": ["Deal.list", "Item.get"]}}, calls=["Deal.list"])
    assert statuses(check_tools_absent(f, {"prefixes": ["Payroll."]})) == {
        "catalog": "pass", "not_attempted": "pass"}


def test_tool_that_exists_or_was_called():
    f = files(after={"tools_list": {"names": ["Payroll.list"]}}, calls=["Payroll.list"])
    assert statuses(check_tools_absent(f, {"prefixes": ["Payroll."]})) == {
        "catalog": "error", "not_attempted": "fail"}


# --- registry helpers ---------------------------------------------------------

def test_a_crashing_check_is_recorded_not_raised():
    task = {"id": "t", "verifiers": [{"name": "deal_set_matches_db", "params": {}}]}
    checks = check_run(files(task=task))
    crashed = [c for c in checks if c.name == "scorer"]
    assert len(crashed) == 1 and crashed[0].status == "error"
    assert {c.verifier for c in checks} >= {"run_completed", "calls_policy", "no_side_effects"}


def test_observations_are_deduplicated_in_order():
    task = {"verifiers": [
        {"name": "deal_set_matches_db", "params": {"section": "at_risk"}},
        {"name": "deal_set_matches_db", "params": {"section": "closing_this_month"}},
        {"name": "quote_matches_db", "params": {"ref": "vice", "qty": 5}},
        {"name": "escalation_effect", "params": {}}]}
    assert verifiers.observations_for(task) == ["sessions_t6", "escalations_t6", "deals",
                                                "item_ref:vice"]


def test_validate_task_rejects_unknown_verifier():
    with pytest.raises(ValueError, match="unknown verifier"):
        verifiers.validate_task({"id": "t", "verifiers": [{"name": "vibes"}]})


# --- LoggedClient -------------------------------------------------------------

def test_logged_client_records_every_call(tmp_path, fake_client):
    inner = fake_client({"Deal.get": lambda a: {"id": a["id"]},
                         "Item.get": [MCPToolError("x", code=NOT_FOUND)]})
    inner.token = "secret"
    client = LoggedClient(inner, tmp_path / "run" / "calls.jsonl")

    assert client.call("Deal.get", {"id": "d1"}) == {"id": "d1"}
    with pytest.raises(MCPToolError):
        client.call("Item.get", {"id": "i1"})

    calls = read_calls(client.path)
    assert [(c["seq"], c["tool"], c["ok"]) for c in calls] == [(1, "Deal.get", True),
                                                               (2, "Item.get", False)]
    assert calls[0]["result_id"] == "d1" and calls[1]["error_code"] == NOT_FOUND
    assert "secret" not in client.path.read_text(encoding="utf-8")
    assert client.token == "secret"


def test_logged_client_records_unexpected_errors(tmp_path, fake_client):
    def boom(args):
        raise RuntimeError("bad")
    client = LoggedClient(fake_client({"Deal.list": boom}), tmp_path / "calls.jsonl")
    with pytest.raises(RuntimeError):
        client.call("Deal.list", {})
    assert read_calls(client.path)[0]["error_code"] == "internal"


def test_read_calls_of_a_missing_file(tmp_path):
    assert read_calls(tmp_path / "nope.jsonl") == []


# --- TaskRun ------------------------------------------------------------------

def test_taskrun_round_trip(tmp_path):
    run = TaskRun(run_id="r1", task_id="pipeline_review", prompt="q", dry_run=True,
                  today="2026-10-05", finding={"quote": {"outcome": "escalated"}},
                  steps=[Step("snapshot_deals", "snapshot_deals", "answered", attempts=2)])
    path = run.save(tmp_path / "r1")

    loaded = TaskRun.load(path)

    assert loaded == run
    assert isinstance(loaded.steps[0], Step) and loaded.steps[0].attempts == 2
    assert not (tmp_path / "r1" / "taskrun.json.tmp").exists()
