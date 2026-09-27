"""Live MCP probes for pipeline bugs. Uses AS + TOKEN from environment. Never prints secrets."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

AS = os.environ.get("AS", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
OUT = Path(__file__).resolve().parent.parent / "docs" / "pipeline-bugs-live.jsonl"

if not AS or not TOKEN:
    print("MISSING_ENV AS or TOKEN", file=sys.stderr)
    sys.exit(1)


def mcp(method: str, params: dict | None = None, id_: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{AS}/api/mcp",
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:500]}
        return {"http_error": e.code, "body": parsed}


def call(name: str, arguments: dict | None = None, id_: int = 1) -> dict:
    return mcp("tools/call", {"name": name, "arguments": arguments or {}}, id_=id_)


def unwrap(result: dict):
    """Normalize tools/call result to python object when possible."""
    if "error" in result:
        return {"_error": result["error"]}
    r = result.get("result") or result
    if isinstance(r, dict) and "content" in r:
        texts = []
        for block in r.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text") or "")
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return {"_text": joined[:2000], "isError": r.get("isError")}
    return r


def summarize(obj, limit=400):
    s = json.dumps(obj, default=str)
    if len(s) > limit:
        return s[:limit] + "..."
    return s


findings: list[dict] = []
log_rows: list[dict] = []


def note(severity: str, title: str, evidence: str, detail: str = ""):
    findings.append({"severity": severity, "title": title, "evidence": evidence, "detail": detail})
    print(f"[{severity}] {title}")
    print(f"  evidence: {evidence}")
    if detail:
        print(f"  detail: {detail[:300]}")


# --- auth check ---
req = urllib.request.Request(
    f"{AS}/api/auth/me",
    headers={"Authorization": f"Bearer {TOKEN}"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    me = json.loads(resp.read().decode("utf-8"))
print("AUTH_OK", me.get("email") or me.get("user", {}).get("email") or list(me.keys())[:5])

# initialize
init = mcp(
    "initialize",
    {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "pipeline-bug-probe", "version": "0.1"},
    },
)
print("INIT", "ok" if "result" in init else summarize(init))

probes = []

# 1) SalesOrder.list pagination
r = call("SalesOrder.list", {})
u = unwrap(r)
log_rows.append({"probe": "SalesOrder.list", "result": u if not isinstance(u, dict) or len(summarize(u)) < 800 else {k: u.get(k) for k in list(u)[:8]}})
if isinstance(u, dict) and not u.get("_error"):
    total = u.get("total")
    data = u.get("data") or u.get("items") or []
    limit = u.get("limit")
    note(
        "P0" if total and limit and total > limit else "INFO",
        "SalesOrder.list pagination",
        f"total={total} returned={len(data)} limit={limit}",
        "No aggregate tool — agent must page",
    )
    sample = data[0] if data else {}
    if sample.get("invoiced_status") == "unbilled":
        note("P0", "Orders stuck unbilled", f"sample id={sample.get('id')} invoiced_status=unbilled", "No Invoice tool to advance")
else:
    note("P0", "SalesOrder.list failed", summarize(u))

# 2) SalesOrder.get with {} — error quality
r = call("SalesOrder.get", {})
u = unwrap(r)
err = u.get("_error") if isinstance(u, dict) else None
# also check isError text path
note(
    "P1",
    "SalesOrder.get empty args error shape",
    summarize(err or u, 500),
    "Expect missing field names for agents",
)
if err:
    msg = json.dumps(err)
    if "missing" not in msg.lower() and "id" not in msg.lower():
        note("P1", "Error does not clearly name missing id", msg[:300])
    elif '"path": "/"' in msg or '"path":"/"' in msg:
        note("P1", "Validation error path is root only", msg[:300], "agents cannot see which field was required")

# 3) Invoice / Payment tools absent — try call
for name in ("Invoice.list", "Payment.list", "SalesOrder.make.Invoice"):
    r = call(name, {})
    u = unwrap(r)
    log_rows.append({"probe": name, "result": summarize(u, 300)})
    if isinstance(u, dict) and (u.get("_error") or u.get("isError") or "Unknown" in summarize(u) or "not found" in summarize(u).lower()):
        note("P0", f"{name} unavailable", summarize(u, 250))

# 4) List pipeline entities — pick samples
samples = {}
for ent in ("Lead", "Deal", "Quotation", "SalesOrder", "Party", "Company"):
    r = call(f"{ent}.list", {"limit": 5})
    u = unwrap(r)
    if isinstance(u, dict) and not u.get("_error"):
        data = u.get("data") or u.get("items") or []
        samples[ent] = data[0] if data else None
        print(f"LIST {ent}: n={u.get('total', len(data))} sample_keys={list((data[0] or {}).keys())[:8] if data else []}")
    else:
        print(f"LIST {ent}: FAIL {summarize(u, 200)}")
        samples[ent] = None

# 5) Dual quote→order paths: compare schemas by dry probing with bogus id
bogus = "00000000-0000-0000-0000-000000000000"
for name in (
    "Quotation.make.SalesOrder",
    "Quotation.convert_to_order.accepted.converted",
    "Quotation.convert_to_order.sent.converted",
    "Quotation.convert_to_order.viewed.converted",
):
    r = call(name, {"id": bogus})
    u = unwrap(r)
    log_rows.append({"probe": name, "result": summarize(u, 400)})
    note("P0", f"Transition tool responds: {name}", summarize(u, 350))

# 6) Stage-skip tools with bogus id — confirm they exist server-side
for name in (
    "Deal.mark_won.new.closed_won",
    "Deal.mark_lost.new.closed_lost",
    "Lead.convert.new.converted",
    "Lead.make.Deal",
):
    r = call(name, {"id": bogus})
    u = unwrap(r)
    log_rows.append({"probe": name, "result": summarize(u, 400)})
    note("P0", f"Stage-skip/convert tool live: {name}", summarize(u, 350))

# 7) If we have a real quotation in sent/viewed — do NOT convert (destructive). Just inspect statuses.
r = call("Quotation.list", {"limit": 20})
u = unwrap(r)
if isinstance(u, dict) and not u.get("_error"):
    data = u.get("data") or []
    by_status = {}
    for q in data:
        st = q.get("status") or q.get("approval_status") or "?"
        by_status[st] = by_status.get(st, 0) + 1
    note("INFO", "Quotation status mix (first page)", json.dumps(by_status))
    # Check if any lack deal_id
    missing_deal = sum(1 for q in data if not q.get("deal_id"))
    if missing_deal:
        note("P1", "Quotations without deal_id", f"{missing_deal}/{len(data)} on first page", "orphan quotes outside deal funnel")

# 8) Deals — stage distribution; products vs contact
r = call("Deal.list", {"limit": 20})
u = unwrap(r)
if isinstance(u, dict) and not u.get("_error"):
    data = u.get("data") or []
    stages = {}
    referral_no_edge = 0
    for d in data:
        stages[d.get("stage") or d.get("status") or "?"] = stages.get(d.get("stage") or d.get("status") or "?", 0) + 1
        if d.get("source") == "referral":
            referral_no_edge += 1  # cannot see referred_by field
    note("INFO", "Deal stage mix (first page)", json.dumps(stages))
    # Check for both party_id and contact_id
    both = sum(1 for d in data if d.get("party_id") and d.get("contact_id") and d.get("party_id") != d.get("contact_id"))
    if both:
        note("P1", "Deals with distinct party_id and contact_id", f"{both}/{len(data)}", "ambiguous primary contact")
    if referral_no_edge:
        note("P1", "Referral deals with no referrer field", f"{referral_no_edge} source=referral on page", "no referred_by_party_id on records")

# 9) PartyRelationship — any referred_by edges?
r = call("PartyRelationship.list", {"limit": 50})
u = unwrap(r)
if isinstance(u, dict) and not u.get("_error"):
    data = u.get("data") or u.get("items") or []
    rel_counts = {}
    for row in data:
        rel = row.get("relationship") or "?"
        rel_counts[rel] = rel_counts.get(rel, 0) + 1
    note("INFO", "PartyRelationship type mix", json.dumps(rel_counts) or "empty")
    if not data:
        note("P1", "PartyRelationship empty", "list returned 0 rows", "edge table unused — observed graph not populated")
    elif "referred_by" not in rel_counts and "referred" not in rel_counts:
        note("P1", "No referral edges in relationship table", json.dumps(rel_counts))

# 10) CallNoteDraft / CampaignAudience / BugReport update
r = call("CallNoteDraft.list", {})
u = unwrap(r)
note("P2", "CallNoteDraft.list", summarize(u, 300) if isinstance(u, dict) and u.get("_error") else f"ok total={u.get('total') if isinstance(u, dict) else '?'}")

r = call("BugReport.update", {"id": bogus, "status": "closed"})
u = unwrap(r)
note("P2", "BugReport.update missing/fails", summarize(u, 300))

r = call("Company.create", {"name": "probe-should-fail"})
u = unwrap(r)
note("P1", "Company.create unavailable", summarize(u, 300))

# 11) Aggregate missing
r = call("SalesOrder.aggregate", {"group_by": ["owner"], "metrics": ["sum:grand_total"]})
u = unwrap(r)
note("P0", "SalesOrder.aggregate missing", summarize(u, 300))

# 12) products vs items on make — schema via empty optional
# Try Deal.make.Quotation with only id bogus
r = call("Deal.make.Quotation", {"id": bogus, "products": [{"item_id": "x", "qty": 1}]})
u = unwrap(r)
s = summarize(u, 500)
note("P1", "Deal.make.Quotation rejects products key?", s, "If error mentions additionalProperties/products, confirms items≠products bug")

r = call("Deal.make.Quotation", {"id": bogus, "items": [{"item_id": "x", "qty": 1}]})
u = unwrap(r)
note("P1", "Deal.make.Quotation with items key", summarize(u, 500))

# Write log (no token)
OUT.write_text("\n".join(json.dumps(x, default=str) for x in log_rows), encoding="utf-8")
print("\n=== FINDINGS SUMMARY ===")
for sev in ("P0", "P1", "P2", "INFO"):
    xs = [f for f in findings if f["severity"] == sev]
    print(f"{sev}: {len(xs)}")
print("WROTE", OUT)
