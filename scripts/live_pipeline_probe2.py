"""Follow-up live probes: tools/list vs call availability, quotation orphans, deal fields."""
from __future__ import annotations

import json
import os
import urllib.request
from collections import Counter

AS = os.environ["AS"].rstrip("/")
TOKEN = os.environ["TOKEN"]


def mcp(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    req = urllib.request.Request(
        f"{AS}/api/mcp",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


def call(name, arguments=None):
    return mcp("tools/call", {"name": name, "arguments": arguments or {}})


def unwrap(result):
    if "error" in result:
        return {"_error": result["error"]}
    r = result.get("result") or {}
    texts = [b.get("text", "") for b in (r.get("content") or []) if b.get("type") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"_text": joined[:1500], "isError": r.get("isError")}


# Fresh tools/list — which transition tools are actually advertised now?
tl = mcp("tools/list", {})
tools = (tl.get("result") or {}).get("tools") or []
names = {t["name"] for t in tools}
print("LIVE_TOOLS", len(names))

interesting = [
    "Deal.mark_won.new.closed_won",
    "Deal.mark_won.negotiation.closed_won",
    "Deal.mark_lost.new.closed_lost",
    "Lead.convert.new.converted",
    "Lead.convert.qualified.converted",
    "Lead.make.Deal",
    "Lead.contact",
    "Quotation.make.SalesOrder",
    "Quotation.convert_to_order.sent.converted",
    "Quotation.convert_to_order.accepted.converted",
    "SalesOrder.aggregate",
    "Invoice.list",
    "BugReport.update",
    "Company.create",
]
print("\n=== advertised vs call ===")
for n in interesting:
    in_list = n in names
    u = unwrap(call(n, {"id": "00000000-0000-0000-0000-000000000000"} if "aggregate" not in n and n not in ("Invoice.list", "Company.create", "SalesOrder.aggregate", "BugReport.update") else {}))
    if n == "BugReport.update":
        u = unwrap(call(n, {"id": "00000000-0000-0000-0000-000000000000"}))
    if n == "Company.create":
        u = unwrap(call(n, {"name": "x"}))
    if n in ("Invoice.list", "SalesOrder.aggregate"):
        u = unwrap(call(n, {}))
    err = (u.get("_error") or {}).get("message", "") if isinstance(u, dict) else ""
    code = (u.get("_error") or {}).get("data", {}).get("code") if isinstance(u, dict) else None
    print(f"{'LIST' if in_list else '----'} {n}")
    print(f"     call: code={code} msg={err[:120]}")

# Quotation orphans detail
u = unwrap(call("Quotation.list", {"limit": 50}))
data = u.get("data") or []
print("\n=== Quotation page ===")
print("total", u.get("total"), "page", len(data))
print("status", dict(Counter(q.get("status") for q in data)))
print("with_deal_id", sum(1 for q in data if q.get("deal_id")))
print("without_deal_id", sum(1 for q in data if not q.get("deal_id")))
print("with_quotation_from_deal_display", sum(1 for q in data if q.get("_deal_id_display")))

# Sales orders linked to quotations?
u = unwrap(call("SalesOrder.list", {"limit": 30}))
sos = u.get("data") or []
print("\n=== SalesOrder page ===")
print("total", u.get("total"))
print("invoiced_status", dict(Counter(s.get("invoiced_status") for s in sos)))
print("with_quotation_id", sum(1 for s in sos if s.get("quotation_id")))
print("delivered_status", dict(Counter(s.get("delivered_status") for s in sos)))

# Deal page — source / party / contact
u = unwrap(call("Deal.list", {"limit": 50}))
deals = u.get("data") or []
print("\n=== Deal page ===")
print("total", u.get("total"), "page", len(deals))
print("stage", dict(Counter(d.get("stage") for d in deals)))
print("source", dict(Counter(d.get("source") for d in deals)))
print("has_products", sum(1 for d in deals if d.get("products")))
print("has_contact_id", sum(1 for d in deals if d.get("contact_id")))
print("has_party_id", sum(1 for d in deals if d.get("party_id")))
print("party_ne_contact", sum(1 for d in deals if d.get("party_id") and d.get("contact_id") and d.get("party_id") != d.get("contact_id")))

# Lead statuses
u = unwrap(call("Lead.list", {"limit": 50}))
leads = u.get("data") or []
print("\n=== Lead page ===")
print("total", u.get("total"))
print("status", dict(Counter(l.get("status") for l in leads)))
print("source", dict(Counter(l.get("source") for l in leads)))

# PartyRelationship total
u = unwrap(call("PartyRelationship.list", {"limit": 1}))
print("\nPartyRelationship.total", u.get("total") if isinstance(u, dict) else u)

# Confirm error quality improvement vs old claim
u = unwrap(call("Deal.get", {}))
print("\nDeal.get {{}} error:", u.get("_error"))
