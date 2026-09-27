"""Recheck week1-three-questions.md claims against tools-list.json."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

root = Path(__file__).resolve().parent.parent
raw = json.loads((root / "tools-list.json").read_text(encoding="utf-8-sig"))
tools = raw.get("result", raw).get("tools", raw if isinstance(raw, list) else [])
names = sorted(t["name"] for t in tools)
by_name = {t["name"]: t for t in tools}

ents: dict[str, set[str]] = defaultdict(set)
for n in names:
    if "." in n and not n.startswith("endpoint."):
        e, op = n.split(".", 1)
        ents[e].add(op)

print("TOOLS", len(tools))
print("ENTITIES", len(ents))

checks = [
    "SalesOrder.list",
    "Quotation.make.SalesOrder",
    "Deal.make.Quotation",
    "AgentSkill.list",
    "AgentSkill.create",
    "AgentPersona.create",
    "AgentRunbook.list",
    "AgentToolPolicy.list",
    "AgentProvider.list",
    "AgentFloorConfig.list",
    "FileAttachment.list",
    "BugReport.create",
    "BugReport.update",
    "endpoint.email.public.subscriber_lists",
    "Goal.list",
    "Goal.create",
    "Pipeline.list",
    "Pipeline.create",
    "Party.create",
    "Note.create",
    "Activity.create",
    "Deal.create",
    "PartyRelationship.list",
    "PartyRelationship.create",
    "PartyRelationship.update",
    "Deal.qualify",
    "Deal.send_proposal",
    "Deal.negotiate",
    "CallNoteDraft.list",
    "AgentEscalation.create",
    "AgentMemory.create",
    "AgentTodo.create",
    "AgentTask.create",
    "job_ledger.verify",
    "job_ledger.replay",
    "job_ledger.forensics",
    "AgentLedgerSeal.list",
    "PrivacyRequest.create",
    "AgentRetentionRule.list",
    "mission_control.staffing_forecast",
    "mission_control.incidents",
    "mission_control.incident_timeline",
    "mission_control.runbooks",
    "AgentSession.create",
    "AgentPersona.list",
    "CRMPreferences.list",
    "CampaignAudience.list",
    "AccountPlan.list",
]

print("\n--- PRESENCE ---")
for c in checks:
    print(f"{'OK' if c in names else 'MISS':4} {c}")

absences = [
    "Invoice",
    "Payment",
    "CreditNote",
    "SalesOrder.make.Invoice",
    "Knowledge.ingest",
    "Ticket.list",
    "Email.send",
    "Sequence.list",
    "ActivityParticipant",
    "DealContact",
    "Party.find_duplicates",
    "Party.merge",
    "Enrichment.resolve",
    "aggregate",
]
print("\n--- SHOULD BE ABSENT ---")
for c in absences:
    hits = [n for n in names if c.lower() in n.lower()]
    print(f"{c}: hits={hits[:8]}")


def props(name: str) -> dict:
    return ((by_name.get(name) or {}).get("inputSchema") or {}).get("properties") or {}


print("\n--- Deal.create ---")
dp = props("Deal.create")
print("keys", sorted(dp))
for k in ("contact_id", "products", "source", "probability", "party_id"):
    if k in dp:
        print(k, json.dumps(dp[k], indent=None)[:300])

print("\n--- Note.create / Activity.create ---")
print("Note", sorted(props("Note.create")))
print("Activity", sorted(props("Activity.create")))

print("\n--- PartyRelationship.create ---")
pr = props("PartyRelationship.create")
print("keys", sorted(pr))
rel = pr.get("relationship") or {}
enum = rel.get("enum") or []
print("enum_count", len(enum))
print("enum", enum)

print("\n--- SalesOrder.create GST-ish ---")
sp = props("SalesOrder.create")
gst_keys = [k for k in sp if any(x in k.lower() for x in ("gst", "hsn", "tds", "tcs", "invoice", "place"))]
print(gst_keys)

print("\n--- AgentSession.create channel ---")
ap = props("AgentSession.create")
print("keys", sorted(ap))
for k in ("channel", "channels", "medium", "source"):
    if k in ap:
        print(k, ap[k])

print("\n--- AgentPersona.list daily_limits? ---")
# check description or schema
persona = by_name.get("AgentPersona.list") or {}
print("desc", (persona.get("description") or "")[:200])
persona_get = by_name.get("AgentPersona.get") or {}
gp = ((persona_get.get("inputSchema") or {}).get("properties") or {})
# look at output if any - often not present; scan create for related
for t in tools:
    if "Persona" in t["name"]:
        p = ((t.get("inputSchema") or {}).get("properties") or {})
        if any("limit" in k.lower() or "budget" in k.lower() or "token" in k.lower() for k in p):
            print(t["name"], [k for k in p if any(x in k.lower() for x in ("limit", "budget", "token", "daily"))])

print("\n--- CallNoteDraft ops ---")
print([n for n in names if "CallNoteDraft" in n])

print("\n--- privacy / forget ---")
print([n for n in names if any(x in n.lower() for x in ("privacy", "forget"))])

print("\n--- job_ledger / mission_control ---")
print([n for n in names if n.startswith("job_ledger.") or n.startswith("mission_control.")])

print("\n--- email tools ---")
print([n for n in names if "email" in n.lower()])

print("\n--- payment/invoice/storefront ---")
print([n for n in names if any(x in n.lower() for x in ("invoice", "payment", "credit", "storefront", "checkout"))])

print("\n--- Deal ops ---")
print([n for n in names if n.startswith("Deal.")])

print("\n--- CRM entity ops ---")
crm = [
    "Lead",
    "Deal",
    "Quotation",
    "SalesOrder",
    "Party",
    "Company",
    "Note",
    "Activity",
    "PartyRelationship",
    "Item",
    "CampaignAudience",
    "Goal",
    "Pipeline",
    "AccountPlan",
    "BugReport",
    "CallNoteDraft",
    "FileAttachment",
    "CRMPreferences",
]
for e in crm:
    print(f"{e}: {sorted(ents.get(e, set()))}")

print("\n--- Agent governance write? ---")
for e in [
    "AgentSkill",
    "AgentPersona",
    "AgentRunbook",
    "AgentToolPolicy",
    "AgentProvider",
    "AgentFloorConfig",
    "AgentJob",
]:
    print(f"{e}: {sorted(ents.get(e, set()))}")

# sample SalesOrder from live probe file
so_path = root / "sales-order-list.json"
if so_path.exists():
    so = json.loads(so_path.read_text(encoding="utf-8-sig"))
    # navigate result content
    content = so
    if "result" in so:
        content = so["result"]
    # MCP tools/call often wraps text
    text = None
    if isinstance(content, dict) and "content" in content:
        for block in content["content"]:
            if block.get("type") == "text":
                text = block.get("text")
                break
    data = json.loads(text) if text else content
    if isinstance(data, dict):
        print("\n--- sales-order-list.json ---")
        print("total", data.get("total"), "limit", data.get("limit"), "count", len(data.get("items") or data.get("data") or []))
        items = data.get("items") or data.get("data") or []
        if items:
            sample = items[0]
            keys = sorted(sample.keys()) if isinstance(sample, dict) else []
            print("sample_keys_gst", [k for k in keys if any(x in k.lower() for x in ("gst", "hsn", "tds", "tcs", "invoice", "place"))])
            for k in ("invoiced_status", "gst_treatment", "place_of_supply", "hsn_or_sac"):
                if isinstance(sample, dict) and k in sample:
                    print(k, sample[k])
