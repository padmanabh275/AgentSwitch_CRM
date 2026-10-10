"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

llm.verify.verify_chat_answer / chat_template and chat_loop._checked: the chat
loop's answer is checked against its own tool trace (refuse_not_permitted_mark_lost).
"""
from __future__ import annotations

from llm import chat_loop
from llm.verify import chat_template, verify_chat_answer

DEAL = "4f00455c-fba3-4165-b080-1a261033700d"
Q = f"Mark deal {DEAL} as lost"
DRY_ESC = {"tool": "file_escalation", "outcome": "escalated", "arguments": {},
           "result": {"outcome": "escalated", "escalation_id": None, "dry_run": True,
                      "would_file": {"subject": "T6-Approval required to mark deal lost"}}}
GET = {"tool": "get_deal", "outcome": "answered", "arguments": {"id": DEAL},
       "result": {"outcome": "answered", "id": DEAL,
                  "deal": {"id": DEAL, "title": "T6-chainwalk-deal-224515", "stage": "negotiation"}}}
LIVE = ("I have escalated the request to mark deal T6-chainwalk-deal-224515 "
        f"({DEAL}) as lost to Meera Kulkarni, as this action requires approval.")


def test_live_answer_fails_on_false_escalation_and_missing_refusal():
    out = verify_chat_answer(LIVE, Q, [GET, DRY_ESC])
    assert not out["ok"] and "dry run" in out["false_escalation"] and out["missing"]


def test_honest_dry_run_answer_passes():
    ans = "I can't mark deals lost from this seat. Dry run: an escalation would be filed; nothing was filed."
    assert verify_chat_answer(ans, Q, [GET, DRY_ESC])["ok"]


def test_real_escalation_may_be_claimed():
    real = {**DRY_ESC, "result": {"outcome": "escalated", "number": "ESC-2026-00071", "dry_run": False}}
    assert verify_chat_answer("I can't do that; I have escalated it as ESC-2026-00071.", Q, [GET, real])["ok"]


def test_invented_escalation_number_fails():
    real = {**DRY_ESC, "result": {"outcome": "escalated", "number": "ESC-2026-00071", "dry_run": False}}
    out = verify_chat_answer("I can't; filed as ESC-2026-00099.", Q, [GET, real])
    assert out["unsupported_escalations"] == ["ESC-2026-00099"]


def test_claiming_escalation_with_no_call_fails():
    out = verify_chat_answer("I can't do that, so I have escalated it.", Q, [GET])
    assert "no escalation was filed" in out["false_escalation"]


def test_not_found_counts_as_saying_so():
    refused = {"tool": "get_deal", "outcome": "refused", "arguments": {"id": DEAL},
               "result": {"outcome": "refused", "reason": "not found"}}
    assert verify_chat_answer(f"Deal {DEAL} does not exist.", f"Look up deal {DEAL}", [refused])["ok"]


def test_template_says_cant_and_dry_run_with_no_names():
    text = chat_template([GET, DRY_ESC])
    assert text.startswith("I can't") and "nothing was filed" in text and "Meera" not in text
    assert verify_chat_answer(text, Q, [GET, DRY_ESC])["ok"]


def test_checked_rewrites_once_then_falls_back(monkeypatch):
    replies = iter([{"text": LIVE}])
    monkeypatch.setattr(chat_loop, "call_llm", lambda *a, **k: next(replies))
    answer, check = chat_loop._checked(Q, LIVE, [], [GET, DRY_ESC])
    assert check["source"] == "template" and answer == chat_template([GET, DRY_ESC])


def test_checked_keeps_a_good_rewrite(monkeypatch):
    good = "I can't mark deals lost. Dry run: an escalation would be filed; nothing was filed."
    monkeypatch.setattr(chat_loop, "call_llm", lambda *a, **k: {"text": good})
    assert chat_loop._checked(Q, LIVE, [], [GET, DRY_ESC]) == (
        good, {"source": "llm_rewrite", "problems": verify_chat_answer(LIVE, Q, [GET, DRY_ESC])})
