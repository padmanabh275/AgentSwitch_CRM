"""domain/items.py: item resolution, the BOM price and the quote path."""
from __future__ import annotations

import pytest

from domain import items
from transport.mcp_client import FORBIDDEN, NOT_FOUND, MCPToolError

UUID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"


def item(id="i1", name="Bench Vice 4in", code="BV-4", **extra):
    return {"id": id, "name": name, "code": code, "uom": "Nos", **extra}


def section(**fields):
    return {"outcome": "answered", "item_id": "i1", "item": item(**fields)}


def item_search(*catalogue):
    """An Item.list handler doing the platform's substring search over name and code."""
    def handler(args):
        term = args["search"].casefold()
        rows = [r for r in catalogue
                if term in r["name"].casefold() or term in r["code"].casefold()]
        return {"data": rows[:args["limit"]], "total": len(rows)}
    return handler


# --- _search_terms ----------------------------------------------------------

@pytest.mark.parametrize("ref, terms", [
    ("vices", ["vices", "vice"]),
    ("benches", ["benches", "bench"]),
    ("boxes", ["boxes", "box"]),
    ("classes", ["classes", "class"]),
    ("glass", ["glass"]),
    ("bolt", ["bolt"]),
])
def test_search_terms(ref, terms):
    assert items._search_terms(ref) == terms


# --- get_item ---------------------------------------------------------------

def test_get_item_keeps_only_item_fields(fake_client):
    client = fake_client({"Item.get": lambda a: item(standard_rate=12.5, secret="x")})
    out = items.get_item(client, "i1")
    assert out["outcome"] == "answered"
    assert out["item"]["standard_rate"] == 12.5
    assert set(out["item"]) == set(items.ITEM_FIELDS)


def test_get_item_not_found_is_refused(fake_client):
    client = fake_client({"Item.get": [MCPToolError("x", code=NOT_FOUND)]})
    assert items.get_item(client, "i1")["outcome"] == "refused"


def test_get_item_other_error_raises(fake_client):
    client = fake_client({"Item.get": [MCPToolError("x", code=FORBIDDEN)]})
    with pytest.raises(MCPToolError):
        items.get_item(client, "i1")


# --- resolve_item -----------------------------------------------------------

def test_resolve_uuid_goes_straight_to_get(fake_client):
    client = fake_client({"Item.get": lambda a: item(id=a["id"])})
    out = items.resolve_item(client, f"  {UUID} ")
    assert client.tools_called() == ["Item.get"]
    assert out["matched_by"] == "id"
    assert out["item_id"] == UUID


def test_resolve_exact_name_among_several(fake_client):
    client = fake_client({"Item.list": item_search(
        item("i1", "Bench Vice"), item("i2", "Bench Vice 4in", "BV-4"))})
    out = items.resolve_item(client, "bench vice")
    assert out["outcome"] == "answered"
    assert out["item_id"] == "i1"
    assert out["matched_by"] == "exact"


def test_resolve_exact_code_case_insensitive(fake_client):
    client = fake_client({"Item.list": item_search(
        item("i1", "Bench Vice", "BV-3"), item("i2", "Bench Vice 4in", "BV-4"))})
    assert items.resolve_item(client, "bv-4")["item_id"] == "i2"


def test_resolve_single_partial_match(fake_client):
    client = fake_client({"Item.list": item_search(item("i1", "Toolmaker's Vice 105mm"))})
    out = items.resolve_item(client, "toolmaker")
    assert out["item_id"] == "i1"
    assert out["matched_by"] == "search"


def test_resolve_retries_plural_in_singular(fake_client):
    client = fake_client({"Item.list": item_search(item("i1", "Bench Vice"))})
    out = items.resolve_item(client, "bench vices")
    assert [a["search"] for _, a in client.calls] == ["bench vices", "bench vice"]
    assert out["item_id"] == "i1"


def test_resolve_no_match_is_refused(fake_client):
    client = fake_client({"Item.list": item_search(item("i1", "Bench Vice"))})
    out = items.resolve_item(client, "lathe")
    assert out["outcome"] == "refused"
    assert out["item_id"] is None
    assert out["reason"] == "no item matches 'lathe' — nothing to quote"


def test_resolve_ambiguous_lists_candidates(fake_client):
    catalogue = [item(f"i{n}", f"Hex Bolt M8x{n}", f"HB-{n}") for n in range(20, 27)]
    client = fake_client({"Item.list": item_search(*catalogue)})

    out = items.resolve_item(client, "hex bolt")

    assert out["outcome"] == "refused"
    assert len(out["candidates"]) == 7
    assert out["reason"].startswith("'hex bolt' matches 7 items — which one? Hex Bolt M8x20 (HB-20);")
    assert out["reason"].endswith("and 2 more")


def test_resolve_two_exact_matches_is_still_ambiguous(fake_client):
    client = fake_client({"Item.list": item_search(
        item("i1", "Bench Vice", "BV-A"), item("i2", "Bench Vice", "BV-B"))})
    out = items.resolve_item(client, "Bench Vice")
    assert out["outcome"] == "refused"
    assert [c["id"] for c in out["candidates"]] == ["i1", "i2"]


# --- price_lookup -----------------------------------------------------------

def test_price_lookup_quotes_from_standard_rate():
    sec = section(standard_rate="123.457", default_bom_id="b1",
                  standard_rate_updated_at="2026-09-20", default_rate=150, mrp=None)
    out = items.price_lookup(sec, 3)

    assert out["outcome"] == "quoted"
    assert out["unit_price"] == 123.457
    assert out["total_price"] == 370.37
    assert out["price_source"] == "Item.standard_rate"
    assert out["price_as_of"] == "2026-09-20"
    assert out["list_prices"] == {"default_rate": 150}  # unset ones are left out
    assert "updated 2026-09-20" in out["reason"]
    assert out["item_name"] == "Bench Vice 4in" and out["item_code"] == "BV-4"


def test_price_lookup_never_quotes_from_list_price():
    out = items.price_lookup(section(standard_rate=None, default_bom_id=None,
                                     selling_price=99), 500)
    assert out["outcome"] == "unpriced"
    assert "total_price" not in out
    assert out["list_prices"] == {"selling_price": 99}
    assert "has no BOM" in out["reason"]


def test_price_lookup_bom_without_price():
    out = items.price_lookup(section(standard_rate=0, default_bom_id="b1"), 500)
    assert out["outcome"] == "unpriced"
    assert "has a BOM but no BOM price" in out["reason"]


@pytest.mark.parametrize("qty", [0, -5, 2.5, "500", None])
def test_price_lookup_refuses_bad_quantity(qty):
    out = items.price_lookup(section(standard_rate=10), qty)
    assert out["outcome"] == "refused"
    assert out["requested_qty"] == qty


# --- escalate_quote / attempt_quote -----------------------------------------

def test_escalate_quote_carries_the_quote_context(fake_client):
    client = fake_client({"AgentEscalation.list": lambda a: {"data": []}})
    priced = items.price_lookup(section(default_bom_id=None), 500)

    out = items.escalate_quote(client, "s1", priced, dry_run=True)

    assert out["outcome"] == "escalated"
    assert out["would_file"]["subject"] == "T6-BOM quote for 500x i1"
    assert out["reason"] == priced["reason"]
    assert out["requested_qty"] == 500 and out["item_name"] == "Bench Vice 4in"


def test_attempt_quote_priced_item_does_not_escalate(fake_client):
    client = fake_client({"Item.list": item_search(item(standard_rate=10, default_bom_id="b"))})
    out = items.attempt_quote(client, "s1", "bench vice 4in", 500)
    assert out["outcome"] == "quoted" and out["total_price"] == 5000.0
    assert "AgentEscalation.list" not in client.tools_called()


def test_attempt_quote_unpriced_item_escalates(fake_client):
    client = fake_client({"Item.list": item_search(item()),
                          "AgentEscalation.list": lambda a: {"data": []},
                          "AgentEscalation.create": lambda a: {"id": "e1", "status": "open"}})
    out = items.attempt_quote(client, "s1", "bench vice 4in", 500)
    assert out["outcome"] == "escalated" and out["escalation_id"] == "e1"
    assert client.tools_called()[-1] == "AgentEscalation.create"


def test_attempt_quote_unresolved_item_keeps_the_quantity(fake_client):
    client = fake_client({"Item.list": item_search()})
    out = items.attempt_quote(client, "s1", "lathe", 500)
    assert out["outcome"] == "refused" and out["requested_qty"] == 500
