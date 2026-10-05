"""llm/narrate.py: the template answer and the verify-then-rewrite loop. The LLM is scripted."""
from __future__ import annotations

import pytest

from llm import narrate
from llm.verify import feedback, verify_claims
from transport.llm_client import LLMError

RULE = "open and expected_close_date before today"


def risk_section(**extra):
    return {"outcome": "answered", "rule": RULE, "deal_ids": ["late"],
            "deals": [{"id": "late", "title": "Pump order", "value": 50000.0, "stage": "proposal",
                       "expected_close_date": "2026-10-02", "days_overdue": 3}],
            "reasons": {"late": "expected_close_date 2026-10-02 passed (3 days ago)"},
            "total_value": 50000.0, "open_without_close_date_ids": ["u1", "u2"],
            "pagination_complete": True, **extra}


def finding(**sections):
    return {"closing_this_month": {"outcome": "not_requested"},
            "at_risk": risk_section(),
            "quote": {"outcome": "escalated", "dry_run": True, "reason": "item Vice has no BOM"},
            "refusals": [], "not_handled": None, **sections}


HONEST = ("1 deal is at risk, worth ₹50,000.00. 2 open deals have no close date. "
          "This was a dry run, so nothing was filed.")
INVENTED = "About ₹6.8 million is at risk."


# --- render_template ----------------------------------------------------------

def test_template_at_risk_section():
    text = narrate.render_template(finding())
    assert f"At risk ({RULE}): 1 deal(s), total ₹50,000.00." in text
    assert "Pump order — ₹50,000.00, stage proposal, close 2026-10-02" in text
    assert "(expected_close_date 2026-10-02 passed (3 days ago))" in text
    assert "2 open deal(s) have no expected close date" in text


def test_template_skips_sections_not_requested():
    assert "Closing" not in narrate.render_template(finding())


def test_template_dry_run_escalation_says_nothing_filed():
    text = narrate.render_template(finding())
    assert "Dry run" in text and "nothing was filed" in text and "No price invented" in text


def test_template_real_escalation_gives_the_number():
    f = finding(quote={"outcome": "escalated", "reason": "no BOM", "number": "ESC-2026-00071",
                       "reused_existing": True})
    assert "Escalated as ESC-2026-00071 (existing open escalation reused)" in narrate.render_template(f)


def test_template_quoted():
    f = finding(quote={"outcome": "quoted", "requested_qty": 500, "item_name": "Vice",
                       "unit_price": 10.0, "total_price": 5000.0, "reason": "BOM price"})
    assert "Quote: 500 x Vice at ₹10.00 = ₹5,000.00 (BOM price)." in narrate.render_template(f)


def test_template_mixed_currency_total_is_na():
    f = finding(at_risk=risk_section(total_value=None))
    assert "total n/a" in narrate.render_template(f)


def test_template_flags_an_incomplete_list():
    f = finding(at_risk=risk_section(pagination_complete=False))
    assert "may be incomplete" in narrate.render_template(f)


def test_template_failed_section_gives_the_reason():
    f = finding(closing_this_month={"outcome": "error", "reason": "gateway timeout"})
    assert "Closing this month: error — gateway timeout" in narrate.render_template(f)


def test_template_refusals_and_not_handled():
    f = finding(refusals=[{"request": "pay my commission", "reason": "payroll"}],
                not_handled={"request": "who owns deal 42?", "reason": "ask on its own"})
    text = narrate.render_template(f)
    assert "Refused: pay my commission — payroll" in text
    assert "Not answered: who owns deal 42? — ask on its own" in text


# --- narrate: the LLM loop ----------------------------------------------------

class ScriptedLLM:
    """Stands in for call_llm: returns (or raises) each scripted reply in turn."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def llm(monkeypatch):
    def install(*replies):
        scripted = ScriptedLLM(*replies)
        monkeypatch.setattr(narrate, "call_llm", scripted)
        return scripted
    return install


def text(t, stop="end_turn"):
    return {"text": t, "stop_reason": stop}


def test_unreachable_llm_uses_the_template(llm):
    llm(LLMError("glc_v5 unreachable"))
    out = narrate.narrate("q", finding()).data
    assert out["source"] == "template"
    assert out["text"] == narrate.render_template(finding())
    assert out["fallback_reason"] == "glc_v5 unreachable"


def test_honest_answer_is_used(llm):
    llm(text(HONEST))
    out = narrate.narrate("q", finding()).data
    assert out["source"] == "llm" and out["text"] == HONEST
    assert out["verification"]["ok"] is True


def test_required_facts_are_asked_for_up_front(llm):
    scripted = llm(text(HONEST))
    narrate.narrate("q", finding())
    prompt = scripted.calls[0][-1]["content"]
    assert "Your answer must state:" in prompt
    assert "at_risk total 50,000.00" in prompt


def test_bad_answer_gets_one_rewrite_with_feedback(llm):
    scripted = llm(text(INVENTED), text(HONEST))
    out = narrate.narrate("q", finding()).data

    assert out["source"] == "llm" and len(out["attempts"]) == 2
    retry = scripted.calls[1]
    assert retry[-2] == {"role": "assistant", "content": INVENTED}
    assert retry[-1]["content"].startswith("Your answer doesn't match the FINDING.")


def test_two_bad_answers_fall_back_to_the_template(llm):
    llm(text(INVENTED), text(INVENTED))
    out = narrate.narrate("q", finding()).data
    assert out["source"] == "template"
    assert out["fallback_reason"] == "llm answer failed verification 2 times"
    assert len(out["attempts"]) == 2


@pytest.mark.parametrize("reply", [text(HONEST, stop="max_tokens"), text(""), {"text": None}])
def test_cut_off_or_empty_answer_uses_the_template(llm, reply):
    llm(reply)
    assert narrate.narrate("q", finding()).data["source"] == "template"


# --- feedback -----------------------------------------------------------------

def test_feedback_lists_each_problem():
    msg = feedback(verify_claims(INVENTED + " See ESC-2026-00099.", finding()))
    assert "remove them: ESC-2026-00099" in msg
    assert "These amounts are not in the FINDING: 6.80" in msg
    assert "You must also state: at_risk total 50,000.00" in msg


def test_feedback_for_omissions_only():
    msg = feedback(verify_claims("Something is at risk.", finding()))
    assert "remove them" not in msg and "amounts are not" not in msg
    assert "dry run: no escalation was actually filed" in msg
