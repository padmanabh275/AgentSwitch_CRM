"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

The unpriced route (draft_for_approval) refuses rather than invent a rate
when the live Quotation.create schema requires items[].rate, and still
drafts with no rate if the platform stops requiring one. No placeholder
rate (e.g. 0) is ever sent by default. See docs/TO_REVISIT.md #11. Since
Release 13 (N480) the live schema needs no rate; the route itself is tested
in test_margin_approval_route.py, and this refusal stays as the guard if
the requirement comes back.
"""
from __future__ import annotations

import pytest

from domain import quotations

DEAL = {"id": "d1", "title": "T6-harness-x", "stage": "negotiation", "party_id": "p1",
        "company_id": "c1", "updated_at": "2026-10-16T10:00:00"}
PRICED = {"item_id": "i1", "item_name": "Lathe Dog", "requested_qty": 500,
          "outcome": "unpriced", "reason": "standard_rate redacted"}


def create_tool(rate_required: bool) -> dict:
    line = {"type": "object", "properties": {"qty": {}, "rate": {}},
            "required": ["qty", "rate"] if rate_required else ["qty"]}
    return {"name": "Quotation.create",
            "inputSchema": {"properties": {"items": {"type": "array", "items": line}}}}


class Client:
    def __init__(self, rate_required: bool):
        self.tools = [create_tool(rate_required)]
        self.calls: list[str] = []

    def list_tools(self):
        return self.tools

    def call(self, tool, args=None):
        self.calls.append(tool)
        if tool == "Deal.get":
            return dict(DEAL)
        if tool == "Quotation.list":
            return {"data": []}
        if tool == "Quotation.create":
            assert all("rate" not in line for line in args["items"]), "a rate was sent"
            return {"id": "q1"}
        if tool == "Quotation.get":
            return {"id": "q1", "status": "draft", "deal_id": "d1",
                    "items": [{"item_id": "i1", "qty": 500, "rate": None}]}
        raise AssertionError(f"unexpected tool call {tool}")


@pytest.mark.parametrize("dry_run", [False, True])
def test_rate_required_refuses_and_writes_nothing(dry_run):
    c = Client(rate_required=True)
    out = quotations.draft_for_approval(c, {"deal": dict(DEAL)}, PRICED, "r1", dry_run=dry_run)
    assert out["outcome"] == "refused"
    assert out["rate_required"] is True
    assert "invented" in out["reason"]
    assert "Quotation.create" not in c.calls
    assert c.calls == ["Deal.get", "Quotation.list"]   # re-read and duplicate check, no write


def test_rate_optional_drafts_without_a_rate():
    c = Client(rate_required=False)
    out = quotations.draft_for_approval(c, {"deal": dict(DEAL)}, PRICED, "r1")
    assert out["outcome"] == "drafted"
    assert "Quotation.create" in c.calls


def test_closed_deal_is_refused_for_the_deal_not_the_rate():
    c = Client(rate_required=True)
    closed = {**DEAL, "stage": "closed_lost", "updated_at": "2026-10-16T10:05:00"}
    c.call = lambda tool, args=None: (c.calls.append(tool), closed)[1]
    out = quotations.draft_for_approval(c, {"deal": dict(DEAL)}, PRICED, "r1")
    assert out["outcome"] == "refused"
    assert "rate_required" not in out


def test_schema_without_quotation_create_is_not_treated_as_required():
    c = Client(rate_required=True)
    c.tools = []
    assert quotations.line_rate_required(c) is False
