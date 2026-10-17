"""AI-WRITTEN. Not a graded hand-written test.

An unpriced quote is escalated only when this seat can file one. When
AgentEscalation.create is absent, refused is the boundary: no price, nobody paged.
"""
from __future__ import annotations

from pathlib import Path

from harness.verifiers import RunFiles, check_expectations, check_quote

ITEM = "e7e982cd-dd11-4f31-b912-3f0a9903f6ed"


def files(quote, names=None, calls=()):
    item = {"kind": "get", "row": {
        "id": ITEM, "name": "Vice", "code": "ST-VICE-150",
        "standard_rate": None, "default_bom_id": "bom-1",
        "_redacted_fields": ["standard_rate"],
    }}
    after = {f"item_ref:{ITEM}": item}
    if names is not None:
        after["tools_list"] = {"names": names}
    task = {"id": "quote_bom_unpriced",
            "expect": {"sections": {"quote": "escalated"}}}
    return RunFiles(
        run_dir=Path("."), task=task,
        taskrun={"ended": "done", "dry_run": False, "finding": {"quote": quote}},
        graph=None, chat_trace=None, calls=list(calls),
        before={"observations": {f"item_ref:{ITEM}": item}},
        after={"observations": after},
    )


def statuses(checks):
    return {c.name: c.status for c in checks}


REFUSED = {
    "outcome": "refused", "item_id": ITEM, "bom_price_redacted": True,
    "reason": "BOM price is hidden; AgentEscalation.create isn't available to this seat",
}


def test_missing_escalation_tool_expects_a_refusal():
    f = files(REFUSED, names=["Item.list", "Deal.list"])
    assert statuses(check_quote(f, {"ref": ITEM, "qty": 500}))["outcome"] == "pass"
    assert statuses(check_expectations(f, {}))["section:quote"] == "pass"


def test_escalation_tool_still_expects_an_escalation():
    f = files(REFUSED, names=["Item.list", "AgentEscalation.create"])
    assert statuses(check_quote(f, {"ref": ITEM, "qty": 500}))["outcome"] == "fail"


def test_transport_rejection_counts_when_the_catalog_was_not_recorded():
    call = {"tool": "AgentEscalation.create", "ok": False, "error_code": "tool_not_available"}
    f = files(REFUSED, calls=[call])
    assert statuses(check_quote(f, {"ref": ITEM, "qty": 500}))["outcome"] == "pass"


def test_named_deal_stays_unpriced_even_when_the_tool_is_absent():
    f = files({"outcome": "unpriced", "item_id": ITEM, "bom_price_redacted": True},
              names=["Item.list"])
    assert statuses(check_quote(f, {"ref": ITEM, "qty": 500, "unpriced_outcome": "unpriced"}))["outcome"] == "pass"
