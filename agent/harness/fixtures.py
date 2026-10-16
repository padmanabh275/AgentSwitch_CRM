"""Fixture rows the write tasks run against, made by the harness itself.

The write tasks never point the agent at a deal another team owns. For
each run the harness builds its own open deal through the path walked on
2026-09-22 (SALES_AGENT_PATH.md): Lead.create -> contact -> qualify ->
Lead.make.Deal -> qualify -> send_proposal -> negotiate. The deal is
`T6-harness-<run_id>`, has no expected close date (so it can't enter the
closing / at-risk lists by date). The priced kind carries one line of the
item; the unpriced kind carries no products (there's no rate to put on one).

Kinds (task field "fixture"):
  open_deal_priced_item   a deal at `negotiation` plus a sellable item
                          whose standard_rate (the BOM price) is set
  open_deal_unpriced_item a deal at `negotiation` plus a sellable item with
                          a BOM whose standard_rate this seat can't see
                          (redacted since 2026-10-15)

Interference (task field "interference"), applied by the runner while
the agent waits at harness/gate.py's "before_quotation_write" gate:
  close_deal_before_write moves the fixture deal to closed_lost

After the after-read, retire() takes our quotations on the fixture deal
out of the margin-approval queue (revise_quote: pending_approval -> draft),
so no approver is left pricing a test quote, then marks a still-open
fixture deal closed_lost, so it doesn't sit in the shared book as an open
undated deal. Nothing is deleted (the seat has no delete, and we don't tidy up).

Everything here uses the harness's own client, so none of it appears in
the agent's calls.jsonl.
"""
from __future__ import annotations

import time

from domain.deals import OPEN_STAGES

FIXTURE_KINDS = ("open_deal_priced_item", "open_deal_unpriced_item")
INTERFERENCE_KINDS = ("close_deal_before_write",)
FIXTURE_QTY = 500
ITEM_PAGE = 50
ITEM_MAX_PAGES = 20


def _unwrap(result: dict) -> dict:
    # {status, result} at minimum; Lead.make.Deal without products carries
    # more keys than that (live, 2026-10-15), so key on "no id at the top".
    if isinstance(result, dict) and "id" not in result \
            and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def _all_items(client) -> list[dict]:
    rows, offset = [], 0
    for _ in range(ITEM_MAX_PAGES):
        page = client.call("Item.list", {"limit": ITEM_PAGE, "offset": offset})
        batch = page.get("data") or []
        rows += batch
        offset += len(batch)
        if not batch or (page.get("total") is not None and offset >= page["total"]):
            break
    return rows


def _sellable(row: dict) -> bool:
    # is_sellable comes back as the string "0" or "1" (live, 2026-10-15), and "0" is truthy.
    return str(row.get("is_sellable")) in ("1", "True", "true")


def _unpriced_item(client) -> dict:
    """The first sellable item with a BOM but no readable standard_rate, by code."""
    rows = [r for r in _all_items(client)
            if _sellable(r) and r.get("default_bom_id") and not r.get("standard_rate")]
    if not rows:
        raise RuntimeError("fixture: no sellable item with a BOM and no visible standard_rate")
    return sorted(rows, key=lambda r: r.get("code") or r["id"])[0]


def _priced_item(client) -> dict:
    """The first sellable item with a BOM price, by code. Picked at run
    time: a hard-coded item's standard_rate can be cleared under us (it
    happened to SP2319/3567)."""
    rows = _all_items(client)
    # is_sellable comes back as the string "0" or "1" (live, 2026-10-15), and "0" is truthy.
    priced = [r for r in rows if r.get("standard_rate") and _sellable(r)]
    if not priced:
        raise RuntimeError("fixture: no sellable item with a standard_rate on this instance")
    return sorted(priced, key=lambda r: r.get("code") or r["id"])[0]


def create(client, kind: str, run_id: str) -> dict:
    if kind not in FIXTURE_KINDS:
        raise ValueError(f"unknown fixture {kind!r}; expected one of {FIXTURE_KINDS}")
    party = (client.call("Party.list", {"limit": 1}).get("data") or [None])[0]
    if not party:
        raise RuntimeError("fixture: Party.list returned nothing")
    priced = kind == "open_deal_priced_item"
    item = _priced_item(client) if priced else _unpriced_item(client)
    rate = float(item["standard_rate"]) if priced else None
    title = f"T6-harness-{run_id}"

    lead = client.call("Lead.create", {
        "party_id": party["id"], "company_id": party["company_id"],
        "value": round(rate * FIXTURE_QTY, 2) if priced else 0, "source": "referral",
        "notes": f"{title}: harness fixture for a write task"})
    client.call("Lead.contact", {"id": lead["id"]})
    client.call("Lead.qualify", {"id": lead["id"]})
    make = {"id": lead["id"], "title": title}
    if priced:
        make["products"] = [{"item_id": item["id"], "qty": FIXTURE_QTY, "rate": rate,
                             "amount": round(rate * FIXTURE_QTY, 2),
                             "description": "harness fixture line"}]
    made = client.call("Lead.make.Deal", make)
    deal = _unwrap(made)
    if "id" not in deal:
        raise RuntimeError(f"fixture: Lead.make.Deal returned no deal id: keys {sorted(made)}")
    try:
        for tool in ("Deal.qualify", "Deal.send_proposal", "Deal.negotiate"):
            client.call(tool, {"id": deal["id"]})
        deal = client.call("Deal.get", {"id": deal["id"]})
        if deal.get("stage") != "negotiation":
            raise RuntimeError(f"fixture: deal {deal['id']} ended at {deal.get('stage')!r}, not negotiation")
    except Exception:
        _mark_lost(client, deal["id"])   # don't leave a half-built open deal behind
        raise
    return {
        "kind": kind, "created_at": time.time(),
        "deal_id": deal["id"], "deal_title": title, "deal_stage": deal["stage"],
        "lead_id": lead["id"], "party_id": party["id"], "company_id": party["company_id"],
        "item_id": item["id"], "item_code": item.get("code"), "item_name": item.get("name"),
        "standard_rate": rate, "item_has_bom": bool(item.get("default_bom_id")), "qty": FIXTURE_QTY,
        # The harness and the agent sign in as the same seat, so whoever
        # created the fixture lead is "us" for created_by checks.
        "me": lead.get("created_by"),
    }


def _mark_lost(client, deal_id: str) -> dict:
    deal = client.call("Deal.get", {"id": deal_id})
    stage = deal.get("stage")
    if stage not in OPEN_STAGES:
        return {"tool": None, "stage_before": stage, "stage_after": stage}
    names = {t.get("name") for t in client.list_tools()}
    tool = f"Deal.mark_lost.{stage}.closed_lost"
    if tool not in names:
        raise RuntimeError(f"no {tool} in this seat's catalogue")
    client.call(tool, {"id": deal_id})
    after = client.call("Deal.get", {"id": deal_id})
    return {"tool": tool, "stage_before": stage, "stage_after": after.get("stage"),
            "updated_at": after.get("updated_at")}


def interfere(client, kind: str, fixture: dict) -> dict:
    """Another writer's change, made while the agent waits at the gate."""
    if kind not in INTERFERENCE_KINDS:
        raise ValueError(f"unknown interference {kind!r}; expected one of {INTERFERENCE_KINDS}")
    started = time.time()
    done = _mark_lost(client, fixture["deal_id"])
    return {"kind": kind, "deal_id": fixture["deal_id"], "started_at": started,
            "at": time.time(), **done}


def _revise_pending(client, fixture: dict) -> list[dict]:
    """pending_approval -> draft for our quotations on the fixture deal."""
    out, offset = [], 0
    for _ in range(ITEM_MAX_PAGES):
        page = client.call("Quotation.list", {"limit": ITEM_PAGE, "offset": offset})
        batch = page.get("data") or []
        offset += len(batch)
        for row in batch:
            if row.get("deal_id") != fixture["deal_id"] or row.get("status") != "pending_approval":
                continue
            if fixture.get("me") and row.get("created_by") != fixture["me"]:
                continue
            client.call("Quotation.revise_quote", {"id": row["id"]})
            after = _unwrap(client.call("Quotation.get", {"id": row["id"]}))
            out.append({"quotation_id": row["id"], "number": row.get("number"),
                        "status_after": after.get("status")})
        if not batch or (page.get("total") is not None and offset >= page["total"]):
            break
    return out


def retire(client, fixture: dict) -> dict:
    """Withdraw our pending quotations, then close a fixture deal the run
    left open. Never raises: a failed clean-up is recorded, it doesn't
    change the run's evidence."""
    out: dict = {"at": time.time()}
    try:
        out["revised"] = _revise_pending(client, fixture)
    except Exception as e:
        out["revise_error"] = f"{type(e).__name__}: {e}"[:300]
    try:
        out.update(_mark_lost(client, fixture["deal_id"]))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out
