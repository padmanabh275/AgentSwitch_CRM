"""Rows shaped like the live platform's (read 2026-10-05), run through domain/ and the verifiers.

Each row is trimmed from a real Item.list / Deal.list response and keeps the
platform's noise fields (_permissions, _redacted_fields, ...). On this seat
standard_rate is redacted, so it is always None: no live item can be quoted.
"""
from __future__ import annotations

import datetime as dt

import pytest

from domain import deals, escalation, items, records
from harness.verifiers import _expected_quote, _obs_item_ref
from transport.mcp_client import NOT_FOUND, MCPToolError

TODAY = dt.date(2026, 10, 5)
REDACTED = ["purchase_rate", "standard_rate", "standard_rate_updated_at"]


def live_item(id, code, name, default_bom_id, default_rate, selling_price, mrp,
              is_sellable=1, is_manufactured=0):
    return {"id": id, "number": "ITEM-2026-00000", "code": code, "name": name, "uom": "Nos",
            "is_sellable": is_sellable, "is_manufactured": is_manufactured,
            "default_bom_id": default_bom_id, "standard_rate": None,
            "standard_rate_updated_at": None, "default_rate": default_rate,
            "selling_price": selling_price, "mrp": mrp, "status": "active",
            "suppliers": [], "_permissions": {"write": True, "delete": False},
            "_redacted_fields": REDACTED, "_display": name}


TOOLMAKER_105 = live_item("3d2c8e2e-08bb-493d-ae6a-c23195189f0d", "SP2319/3567 #3567",
                          "Toolmaker's Vice 105mm (Mtr)", "c8852d90-2749-4af7-88a3-e37277efc3da",
                          6861.71, 86663.78, 554228.74)
TOOLMAKER_294 = live_item("fe5e9549-8aef-4711-b5d0-0db8127c6dac", "VC9434/3549 #3549",
                          "Toolmaker's Vice 294mm (Mtr)", "2fa0930b-823f-401d-9d13-f11d1cdc7866",
                          186975.27, 0.0, 299096.28, is_sellable=0)
TOOLMAKER_271 = live_item("34b64850-10f0-457f-a741-292eed3405b5", "PW4266/3539 #3539",
                          "Toolmaker's Vice 271mm (Pair)", "2fa0930b-823f-401d-9d13-f11d1cdc7866",
                          0.0, 497291.77, 10993.28, is_sellable=0)
BENCH_VICE_150 = live_item("bb7dd230-3b18-4d25-9593-255ed551c484", "ST-VICE-150",
                           "SuryaTools Bench Vice 150mm", "3682c64e-fbf3-446f-9be1-62be81c5516f",
                           5799.0, 5799.0, 6999.0, is_manufactured=1)
BENCH_VICE_100 = live_item("e7e982cd-dd11-4f31-b912-3f0a9903f6ed", "ST-VICE-100",
                           "SuryaTools Bench Vice 100mm", None,
                           3499.0, 3499.0, 4499.0, is_manufactured=1)
HEX_BOLT_M10 = live_item("bf35328c-ec4e-43d4-9e8c-94743e51b86e", "RM-BOLT-M10",
                         "Hex Bolt M10x30 Zinc 8.8", None, 8.02, 0.0, 0.0, is_sellable=0)
HEX_BOLT_M8 = live_item("597e4991-71f4-4725-bb8e-707674ec5c68", "RM-BOLT-M8",
                        "Hex Bolt M8x25 Zinc 8.8", "00cfea53-56da-4a54-8541-37df869d87cd",
                        4.96, 0.0, 0.0, is_sellable=0)

CATALOGUE = [TOOLMAKER_105, TOOLMAKER_294, TOOLMAKER_271, BENCH_VICE_150, BENCH_VICE_100,
             HEX_BOLT_M10, HEX_BOLT_M8]

# The 20 newest escalations on the platform: other teams' subjects, all withdrawn.
LIVE_ESCALATIONS = [
    {"id": "79b0e1af-8878-4ecd-95b0-9342ad0763b6", "number": "ESC-2026-00070",
     "subject": "team04-harness task escalate_blocked_wo48", "status": "withdrawn"},
    {"id": "6971e26b-7e66-4a75-81bd-c194498c20e0", "number": "ESC-2026-00060",
     "subject": "T21-DR DF-2026-00001: quality needed", "status": "withdrawn"},
]


def live_search(args):
    """Item.list as the platform serves it: substring over name and code, catalogue order."""
    term = args["search"].casefold()
    rows = [r for r in CATALOGUE if term in r["name"].casefold() or term in r["code"].casefold()]
    return {"data": rows[:args["limit"]], "total": len(rows)}


def live_get(args):
    for r in CATALOGUE:
        if r["id"] == args["id"]:
            return r
    raise MCPToolError("Item.get: Item not found.", code=NOT_FOUND)


def esc_by_subject(args):
    return {"data": [r for r in LIVE_ESCALATIONS if r["subject"] == args["subject"]]}


@pytest.fixture
def platform(fake_client):
    return fake_client({"Item.list": live_search, "Item.get": live_get,
                        "AgentEscalation.list": esc_by_subject})


# --- items: resolving live references ----------------------------------------

def test_bench_vice_is_ambiguous_between_the_two_suryatools_vices(platform):
    out = items.resolve_item(platform, "bench vice")
    assert out["outcome"] == "refused"
    assert [c["code"] for c in out["candidates"]] == ["ST-VICE-150", "ST-VICE-100"]
    assert out["reason"] == ("'bench vice' matches 2 items — which one? SuryaTools Bench Vice "
                             "150mm (ST-VICE-150); SuryaTools Bench Vice 100mm (ST-VICE-100)")


def test_bench_vices_retries_singular_and_is_still_ambiguous(platform):
    out = items.resolve_item(platform, "bench vices")
    assert [a["search"] for _, a in platform.calls] == ["bench vices", "bench vice"]
    assert out["outcome"] == "refused" and len(out["candidates"]) == 2


def test_vice_matches_five_without_and_more(platform):
    out = items.resolve_item(platform, "vice")
    assert out["outcome"] == "refused"
    assert out["reason"].startswith("'vice' matches 5 items — which one? Toolmaker's Vice 105mm")
    assert "more" not in out["reason"]


@pytest.mark.parametrize("ref, item", [
    ("ST-VICE-150", BENCH_VICE_150),
    ("st-vice-100", BENCH_VICE_100),
    ("SuryaTools Bench Vice 100mm", BENCH_VICE_100),
    ("Hex Bolt M8x25 Zinc 8.8", HEX_BOLT_M8),
])
def test_exact_code_or_name_resolves(platform, ref, item):
    out = items.resolve_item(platform, ref)
    assert out["outcome"] == "answered"
    assert out["item_id"] == item["id"]
    assert out["matched_by"] == "exact"


def test_resolved_item_drops_platform_noise(platform):
    out = items.resolve_item(platform, "ST-VICE-150")
    assert set(out["item"]) == set(items.ITEM_FIELDS)
    assert "_redacted_fields" not in out["item"] and "suppliers" not in out["item"]


def test_item_id_goes_to_get(platform):
    out = items.resolve_item(platform, BENCH_VICE_100["id"])
    assert platform.tools_called() == ["Item.get"]
    assert out["item"]["code"] == "ST-VICE-100"


def test_missing_item_id_is_refused(platform):
    missing = "00000000-0000-0000-0000-000000000000"
    out = items.resolve_item(platform, missing)
    assert out["outcome"] == "refused"
    assert out["reason"] == f"item {missing} does not exist — nothing to quote"


# --- items: pricing with standard_rate redacted ------------------------------

def priced(platform, ref, qty=500):
    return items.price_lookup(items.resolve_item(platform, ref), qty)


def test_redacted_rate_with_bom_is_unpriced(platform):
    out = priced(platform, "ST-VICE-150")
    assert out["outcome"] == "unpriced"
    assert "has a BOM but no BOM price" in out["reason"]
    assert "total_price" not in out and "unit_price" not in out


def test_redacted_rate_without_bom_says_no_bom(platform):
    out = priced(platform, "ST-VICE-100")
    assert out["outcome"] == "unpriced"
    assert "has no BOM" in out["reason"]


def test_list_prices_are_context_only_and_zeroes_left_out(platform):
    assert priced(platform, "ST-VICE-150")["list_prices"] == {
        "default_rate": 5799.0, "selling_price": 5799.0, "mrp": 6999.0}
    assert priced(platform, "Toolmaker's Vice 294mm (Mtr)")["list_prices"] == {
        "default_rate": 186975.27, "mrp": 299096.28}
    assert priced(platform, "RM-BOLT-M10")["list_prices"] == {"default_rate": 8.02}


@pytest.mark.parametrize("item", CATALOGUE, ids=lambda i: i["code"])
def test_no_live_item_can_be_quoted(platform, item):
    assert priced(platform, item["id"])["outcome"] == "unpriced"


def test_dry_run_quote_escalates_without_filing(platform):
    out = items.attempt_quote(platform, "s1", "ST-VICE-100", 500, dry_run=True)
    assert out["outcome"] == "escalated"
    assert out["dry_run"] is True and out["escalation_id"] is None
    assert out["would_file"]["subject"] == f"T6-BOM quote for 500x {BENCH_VICE_100['id']}"
    assert "AgentEscalation.create" not in platform.tools_called()


def test_other_teams_withdrawn_escalations_are_not_reused(fake_client):
    client = fake_client({"AgentEscalation.list": lambda a: {"data": LIVE_ESCALATIONS}})
    assert escalation.find_open(client, escalation.quote_subject("x", 1)) is None


# --- verifier agrees with the domain on live references ----------------------

@pytest.mark.parametrize("ref", ["bench vice", "vice", "ST-VICE-150", "ST-VICE-100",
                                 "RM-BOLT-M8", "lathe", BENCH_VICE_150["id"]])
def test_verifier_and_domain_agree_on_the_outcome(platform, ref):
    domain_outcome = items.attempt_quote(platform, "s1", ref, 500, dry_run=True)["outcome"]
    verifier_outcome, _, _ = _expected_quote(_obs_item_ref(platform, ref), ref, 500)
    assert domain_outcome == verifier_outcome


def test_verifier_explains_bom_without_rate(platform):
    _, item, why = _expected_quote(_obs_item_ref(platform, "ST-VICE-150"), "ST-VICE-150", 500)
    assert item["id"] == BENCH_VICE_150["id"]
    assert why == "BOM but no standard_rate"


# --- deals: rows as Deal.list returns them ------------------------------------

def live_deal(id, title, stage, value, currency="INR", close=None, **extra):
    return {"id": id, "title": title, "party_id": "132c9587-724b-460a-b736-46d049602207",
            "stage": stage, "value": value, "currency": currency,
            "expected_close_date": close, "probability": 0.0, "owner": None,
            "pipeline": "oem_new_business", "products": [],
            "updated_at": "2026-09-27T22:03:02.452456", "_base_value": value,
            "_base_currency": "INR", "_rot_days": 7, "_rot_level": "fresh",
            "_permissions": {"write": True, "delete": False}, "_display": title, **extra}


ZZZ_DEAL = live_deal("2573d64d-af43-4ce7-b9c9-a95756a432af", "T6-hunt-B5-0332", "new", 0.0, "ZZZ")
LIVE_DEALS = [
    ZZZ_DEAL,
    live_deal("d8d18b7e-698e-4bba-88f8-848d5b17dd6a", "Deal from 00f7cc88", "new", 25000.0),
    live_deal("4f00455c-fba3-4165-b080-1a261033700d", "T6-chainwalk-deal-224515", "negotiation",
              1000.0, products=[{"item_id": "37fbad5b-1cda-4706-95fd-f4a28981f41b", "qty": 10,
                                 "rate": 100, "amount": 1000.0}]),
    live_deal("42249ec8-28a6-429e-9368-03204cdd28ad", "T6-hunt-B6-0334", "closed_won", 1.0),
    live_deal("2fafccfb-c0ea-4ded-9ed2-9677f9743294", "T6-unhappy-term-230641", "closed_lost", 999.0),
    live_deal("dated-overdue", "Overdue this month", "proposal", 50000.0, close="2026-10-02"),
    live_deal("dated-later", "Later this month", "qualification", 20000.0, close="2026-10-30"),
]


def snapshot(rows):
    return {"deals": rows, "pagination_complete": True, "fetched_at": "2026-10-05T10:00:00+05:30"}


def test_undated_open_deals_are_reported_separately():
    out = deals.at_risk(snapshot(LIVE_DEALS), TODAY)
    assert out["deal_ids"] == ["dated-overdue"]
    assert out["open_without_close_date_ids"] == [ZZZ_DEAL["id"], LIVE_DEALS[1]["id"],
                                                  LIVE_DEALS[2]["id"]]


def test_undated_zzz_deal_does_not_spoil_the_month_total():
    out = deals.closing_this_month(snapshot(LIVE_DEALS), TODAY)
    assert out["deal_ids"] == ["dated-overdue", "dated-later"]
    assert out["total_value"] == 70000.0
    assert out["total_by_currency"] == {"INR": 70000.0}


def test_zzz_deal_due_this_month_blocks_a_single_total():
    rows = LIVE_DEALS + [{**ZZZ_DEAL, "id": "zzz-dated", "expected_close_date": "2026-10-20",
                          "value": 10.0}]
    out = deals.closing_this_month(snapshot(rows), TODAY)
    assert out["total_value"] is None
    assert out["total_by_currency"] == {"INR": 70000.0, "ZZZ": 10.0}


def test_deal_summaries_drop_platform_noise():
    out = deals.closing_this_month(snapshot(LIVE_DEALS), TODAY)
    summary = out["deals"][0]
    assert not any(k.startswith("_") for k in summary)
    assert "products" not in summary and "pipeline" not in summary
    assert deals.at_risk(snapshot(LIVE_DEALS), TODAY)["deals"][0]["days_overdue"] == 3


def test_live_book_size_reads_in_three_pages(fake_client):
    # 143 deals on 2026-10-05: 69 new, 13 qualification, 5 negotiation, 3 proposal,
    # 23 closed_won, 30 closed_lost.
    stages = (["new"] * 69 + ["qualification"] * 13 + ["negotiation"] * 5 + ["proposal"] * 3
              + ["closed_won"] * 23 + ["closed_lost"] * 30)
    rows = [live_deal(f"d{i}", f"Deal {i}", s, 100.0) for i, s in enumerate(stages)]

    def handler(args):
        return {"data": rows[args["offset"]:args["offset"] + args["limit"]], "total": len(rows)}

    client = fake_client({"Deal.list": handler})
    snap = deals.snapshot_deals(client)

    assert [a["offset"] for _, a in client.calls] == [0, 50, 100]
    assert len(snap["deals"]) == 143
    assert snap["pagination_complete"] is True
    assert snap["unknown_stage_ids"] == []
    assert sum(deals.is_open(d) for d in snap["deals"]) == 90


def test_missing_deal_is_refused_with_the_platform_error(fake_client):
    client = fake_client({"Deal.get": [MCPToolError("Deal.get: Deal not found.", code=NOT_FOUND)]})
    out = records.get_deal(client, "00000000-0000-0000-0000-000000000000")
    assert out["outcome"] == "refused"
    assert out["reason"] == "deal 00000000-0000-0000-0000-000000000000 does not exist"
