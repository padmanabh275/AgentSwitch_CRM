"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-04).

Not the team's hand-written tests and not claimed as such: the course scores
a test written by Claude or Codex as zero. See tests_ai/README.md.

llm/provenance.py, and that chat_loop never sends an unseen id to AgentSwitch.
"""
from __future__ import annotations

from llm import chat_loop
from llm.provenance import SeenIds

AT_RISK = {
    "outcome": "answered",
    "deal_ids": ["D-111", "D-333"],
    "deals": [{"id": "D-111", "party_id": "P-222", "title": "Acme gearbox order"},
              {"id": "D-333", "party_id": "P-444", "title": "Bharat spindles"}],
    "reasons": {"D-111": "expected_close_date 2026-10-01 passed"},
}


def seen_after(*results, query="escalate the Acme deal"):
    seen = SeenIds(query)
    for r in results:
        seen.add_result(r)
    return seen


# --- the check --------------------------------------------------------------

def test_party_id_seen_as_a_party_id_passes():
    assert seen_after(AT_RISK).check("file_escalation", {"reason": "r", "party_id": "P-222"}) is None


def test_a_deal_id_in_the_party_slot_is_rejected():
    out = seen_after(AT_RISK).check("file_escalation", {"reason": "r", "party_id": "D-111"})
    assert out["outcome"] == "error" and out["error_code"] == "unseen_id"
    assert "appeared only as deal_ids, deals.id" in out["reason"]


def test_an_invented_id_is_rejected():
    out = seen_after(AT_RISK).check("file_escalation", {"reason": "r", "party_id": "P-999"})
    assert "did not appear in the question or any earlier result" in out["reason"]


def test_an_id_the_user_typed_is_accepted():
    seen = seen_after(query="escalate deal D-777 for party P-888")
    assert seen.check("get_deal", {"id": "D-777"}) is None
    assert seen.check("file_escalation", {"reason": "r", "party_id": "P-888"}) is None


def test_the_question_must_contain_the_whole_id():
    seen = seen_after(query="what about P-8889?")
    assert seen.check("file_escalation", {"reason": "r", "party_id": "P-888"}) is not None
    assert seen.check("get_deal", {"id": "a"}) is not None  # "a" is in "what about"


def test_deal_ids_from_a_list_and_a_record():
    seen = seen_after(AT_RISK)
    assert seen.check("get_deal", {"id": "D-333"}) is None
    assert seen.check("get_deal", {"id": "P-222"}) is not None  # a party, not a deal


def test_lead_id_seen_on_a_deal_record():
    deal = {"outcome": "answered", "id": "D-1", "deal": {"id": "D-1", "lead_id": "L-5"}}
    seen = seen_after(deal)
    assert seen.check("get_lead", {"id": "L-5"}) is None
    assert seen.check("get_deal", {"id": "D-1"}) is None


def test_a_refusal_echoing_the_guess_does_not_launder_it():
    refused = {"outcome": "refused", "id": "D-404", "reason": "deal D-404 does not exist"}
    assert seen_after(refused).check("get_deal", {"id": "D-404"}) is not None


def test_absent_or_blank_party_id_and_other_tools_are_not_checked():
    seen = seen_after()
    assert seen.check("file_escalation", {"reason": "r"}) is None
    assert seen.check("file_escalation", {"reason": "r", "party_id": "  "}) is None
    assert seen.check("attempt_quote", {"item": "vice", "qty": 5}) is None


# --- chat_loop --------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "Deal.list":
            return {"data": [{**d, "stage": "proposal", "expected_close_date": "2026-10-01",
                              "value": 10.0, "currency": "INR"} for d in AT_RISK["deals"]],
                    "total": 2}
        if tool == "AgentEscalation.list":
            return {"data": []}
        raise AssertionError(f"unexpected {tool}")


def test_chat_loop_stops_the_wrong_party_and_lets_the_model_correct_it(monkeypatch):
    def call(i, name, **args):
        return {"tool_calls": [{"id": f"c{i}", "name": name, "arguments": args}]}

    replies = iter([
        call(1, "list_at_risk"),
        call(2, "file_escalation", reason="Acme overdue", party_id="D-111"),  # deal id, wrong slot
        call(3, "file_escalation", reason="Acme overdue", party_id="P-222"),
        {"text": "escalated"},
    ])
    monkeypatch.setattr(chat_loop, "call_llm", lambda messages, **kw: next(replies))
    client = FakeClient()

    _, _, trace = chat_loop.run("escalate the Acme deal", "s1", client, dry_run=True)

    assert [t["outcome"] for t in trace] == ["answered", "error", "escalated"]
    assert trace[1]["result"]["error_code"] == "unseen_id"
    # The rejected call never reached AgentSwitch: one duplicate check, for the corrected call.
    assert client.calls == ["Deal.list", "AgentEscalation.list"]
    assert trace[2]["result"]["would_file"]["party_id"] == "P-222"
