"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

domain.items.price_lookup: a BOM price the platform redacts for this seat is
not an unset one, and the escalation reason must say which (batch 20261005T205145).
"""
from __future__ import annotations

from domain.items import price_lookup


def _item(**kw):
    base = {"id": "i1", "name": "Vice", "code": "V", "default_bom_id": "b1", "standard_rate": None}
    return {"item_id": "i1", "item": {**base, **kw}}


def test_unset_bom_price_says_not_set():
    out = price_lookup(_item(_redacted_fields=[]), 500)
    assert out["outcome"] == "unpriced" and out["bom_price_redacted"] is False
    assert "set yet" in out["reason"]


def test_redacted_bom_price_never_claims_unset():
    out = price_lookup(_item(_redacted_fields=["purchase_rate", "standard_rate"]), 500)
    assert out["outcome"] == "unpriced" and out["bom_price_redacted"] is True
    assert "hidden from this seat" in out["reason"] and "set yet" not in out["reason"]


def test_no_bom_wins_over_redaction():
    out = price_lookup(_item(default_bom_id=None, _redacted_fields=["standard_rate"]), 500)
    assert "has no BOM" in out["reason"]
