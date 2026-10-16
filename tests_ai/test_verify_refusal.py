"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

llm.verify.verify_claims: a refused request must be said as a refusal, not a
deflection (week1_refuse_commission, batch 20261005T215643).
"""
from __future__ import annotations

from llm.verify import verify_claims

FINDING = {"refusals": [{"outcome": "refused", "request": "commission calculation",
                         "reason": "handled by payroll/finance"}]}


def test_deflection_is_missing_the_refusal():
    text = "Commission calculations are handled by the payroll or finance department, not the sales agent."
    out = verify_claims(text, FINDING)
    assert not out["ok"] and any("commission calculation" in m for m in out["missing"])


def test_plain_refusal_passes():
    assert verify_claims("I can't calculate commission: that's handled by payroll/finance.", FINDING)["ok"]


def test_no_refusals_requires_nothing():
    assert verify_claims("All good.", {})["ok"]
