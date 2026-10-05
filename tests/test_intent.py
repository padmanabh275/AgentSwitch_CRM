"""llm/intent.py: validate() over what the classifier would return. No LLM."""
from __future__ import annotations

import pytest

from llm.intent import IntentError, validate


def raw(**fields):
    return {"asks": [], "item_ref": "", "qty": 0, "out_of_scope": [], "other": "", **fields}


# --- asks -------------------------------------------------------------------

def test_unknown_asks_dropped_and_rest_in_canonical_order():
    out = validate(raw(asks=["quote", "bogus", "closing_this_month"]))
    assert out["asks"] == ["closing_this_month", "quote"]


def test_missing_asks_is_empty():
    assert validate({})["asks"] == []


# --- qty and item_ref -------------------------------------------------------

@pytest.mark.parametrize("qty", [0, -3, "500", True, 2.5, None])
def test_bad_qty_becomes_none(qty):
    assert validate(raw(qty=qty))["qty"] is None


def test_positive_qty_is_kept():
    assert validate(raw(qty=500))["qty"] == 500


@pytest.mark.parametrize("ref, expected", [
    ("   ", None),
    ("", None),
    (None, None),
    ("  bench vice ", "bench vice"),
])
def test_item_ref_is_stripped(ref, expected):
    assert validate(raw(item_ref=ref))["item_ref"] == expected


def test_out_of_scope_without_a_request_is_dropped():
    out = validate(raw(out_of_scope=[{"request": "", "reason": "x"}, "not a dict",
                                     {"request": " pay my commission ", "reason": " payroll "}]))
    assert out["out_of_scope"] == [{"request": "pay my commission", "reason": "payroll"}]


# --- routing ----------------------------------------------------------------

def test_any_known_ask_routes_to_graph():
    assert validate(raw(asks=["at_risk"], other="look up deal X"))["route"] == "graph"


def test_only_out_of_scope_routes_to_graph_so_the_refusal_is_recorded():
    out = validate(raw(out_of_scope=[{"request": "pay my commission", "reason": "payroll"}]))
    assert out["route"] == "graph"


def test_other_request_routes_to_chat():
    out = validate(raw(other="look up deal X"))
    assert out["route"] == "chat"
    assert out["other"] == "look up deal X"


def test_out_of_scope_plus_other_routes_to_chat():
    out = validate(raw(out_of_scope=[{"request": "edit payroll", "reason": "hr"}],
                       other="look up deal X"))
    assert out["route"] == "chat"


def test_nothing_classified_routes_to_chat():
    assert validate(raw())["route"] == "chat"


@pytest.mark.parametrize("bad", [[], ["at_risk"], "at_risk", None, 42])
def test_non_dict_raises(bad):
    with pytest.raises(IntentError):
        validate(bad)
