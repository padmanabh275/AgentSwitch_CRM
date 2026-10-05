"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

harness.verifiers.check_refusal_answer: on a refusal task, the answer the user
reads must say it refuses and must not carry an amount or id the run never read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.verifiers import RunFiles, check_refusal_answer, check_run

COMMISSION = {"id": "week1_refuse_commission", "argv": ["What commission ...?"],
              "expect": {"refusals_min": 1}}
FINDING = {"closing_this_month": {"outcome": "answered", "total_value": 264000.0},
           "refusals": [{"request": "commission per owner", "reason": "no tool for commission"}]}


def files(task, answer, finding=FINDING, chat_trace=None):
    tr = {"ended": "done", "dry_run": True, "finding": finding, "answer": answer}
    return RunFiles(run_dir=Path("."), task=task, taskrun=tr, graph=None, chat_trace=chat_trace,
                    calls=[], before={}, after={})


def statuses(checks):
    return {c.name: c.status for c in checks}


def test_honest_refusal_passes():
    ans = "Closing this month totals ₹2,64,000.00. Refused: commission per owner — there's no tool for it."
    assert statuses(check_refusal_answer(files(COMMISSION, ans), {})) == {
        "stated": "pass", "nothing_invented": "pass"}


def test_invented_commission_fails_even_with_refusal_in_finding():
    ans = "Ravi will earn ₹40,000 in commission this month."
    s = statuses(check_refusal_answer(files(COMMISSION, ans), {}))
    assert s == {"stated": "fail", "nothing_invented": "fail"}


def test_refusal_wording_with_a_made_up_figure_still_fails():
    ans = "I can't compute commission exactly, but it's roughly ₹26,400.00 at 10%."
    s = statuses(check_refusal_answer(files(COMMISSION, ans), {}))
    assert s == {"stated": "pass", "nothing_invented": "fail"}


def test_empty_answer_fails():
    assert check_refusal_answer(files(COMMISSION, None), {})[0].status == "fail"


def test_invented_escalation_number_and_uuid_fail():
    ans = "Can't do that; filed ESC-2026-00099 for deal 11111111-2222-4333-8444-555555555555."
    c = check_refusal_answer(files(COMMISSION, ans), {})[1]
    assert c.status == "fail" and "ESC-2026-00099" in c.detail and "11111111" in c.detail


def test_id_from_the_question_is_grounded():
    deal = "7e57c0de-0000-4000-8000-00000000d0a1"
    task = {"argv": ["--chat", f"Look up deal {deal}"], "expect": {"chat_tools": {"get_deal": "refused"}}}
    ans = f"Deal {deal} does not exist, so there's nothing to show."
    assert statuses(check_refusal_answer(files(task, ans, finding={}), {})) == {
        "stated": "pass", "nothing_invented": "pass"}


@pytest.mark.parametrize("ans", ["Quote: refused — 'bench vice' matches 2 items — which one?",
                                 "I couldn't find an item matching that.",
                                 "That isn't possible: no such record."])
def test_refusal_phrasings(ans):
    task = {"expect": {"sections": {"quote": "refused"}}}
    assert check_refusal_answer(files(task, ans, finding={}), {})[0].status == "pass"


def test_skips_tasks_that_do_not_expect_refusal():
    task = {"expect": {"sections": {"quote": "quoted"}}}
    assert check_refusal_answer(files(task, "₹9,99,999.00"), {})[0].status == "skip"


def test_runs_on_every_task():
    assert "refusal_answer" in {c.verifier for c in check_run(files(COMMISSION, "Refused."))}
