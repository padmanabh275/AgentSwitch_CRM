"""Find pipeline bugs / inconsistencies in tools-list.json CRM funnel."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

root = Path(__file__).resolve().parent.parent
raw = json.loads((root / "tools-list.json").read_text(encoding="utf-8-sig"))
tools = raw.get("result", raw).get("tools", [])
by = {t["name"]: t for t in tools}
names = sorted(by)


def schema(name: str) -> dict:
    return by.get(name, {}).get("inputSchema") or {}


def props(name: str) -> dict:
    return schema(name).get("properties") or {}


def required(name: str) -> list:
    return list(schema(name).get("required") or [])


def enum(name: str, field: str):
    p = props(name).get(field) or {}
    return p.get("enum")


# --- inventory pipeline entities ---
pipeline_entities = [
    "Lead",
    "Deal",
    "Quotation",
    "SalesOrder",
    "Party",
    "Company",
    "Item",
    "Note",
    "Activity",
    "PartyRelationship",
    "CampaignAudience",
    "Goal",
    "Pipeline",
    "AccountPlan",
    "CallNoteDraft",
]

ents: dict[str, list[str]] = defaultdict(list)
for n in names:
    if "." in n and not n.startswith("endpoint."):
        e, op = n.split(".", 1)
        ents[e].append(op)

print("=== PIPELINE ENTITY OPS ===")
for e in pipeline_entities:
    print(f"{e}: {sorted(ents.get(e, []))}")

print("\n=== TRANSITION / MAKE TOOLS ===")
for n in names:
    if any(x in n for x in (".make.", "convert", "qualify", "disqualify", "mark_", "confirm", "accept", "decline", "send", "negotiate", "approval")):
        if n.split(".")[0] in ("Lead", "Deal", "Quotation", "SalesOrder") or "make" in n:
            r = required(n)
            p = sorted(props(n))
            print(f"{n}")
            print(f"  required={r}")
            print(f"  props={p}")

print("\n=== CREATE REQUIRED FIELDS ===")
for e in ("Lead", "Deal", "Quotation", "SalesOrder", "Party", "Note", "Activity", "Item"):
    name = f"{e}.create"
    if name in by:
        print(f"{name}: required={required(name)}")
        print(f"  props={sorted(props(name))}")

# Bug hunts
bugs: list[tuple[str, str, str]] = []

# 1. Deal products array vs contact scalar (already known)
dp = props("Deal.create")
if "products" in dp and dp["products"].get("type") == "array":
    if "contact_id" in dp and dp["contact_id"].get("type") == "string":
        bugs.append(
            (
                "schema",
                "Deal.create",
                "products is array but contact_id/party_id are scalars — buying committee cannot be modeled",
            )
        )

# 2. Referral source without referrer
src = enum("Deal.create", "source") or []
if "referral" in src and "referred_by" not in props("Deal.create") and "referred_by_party_id" not in props("Deal.create"):
    bugs.append(
        (
            "schema",
            "Deal.create",
            f"source enum includes referral ({src}) but no referred_by / referred_by_party_id field",
        )
    )

# Lead source?
ls = enum("Lead.create", "source") or enum("Lead.create", "lead_source")
print("\nLead.create source-like:", {k: props("Lead.create").get(k) for k in props("Lead.create") if "source" in k.lower() or "refer" in k.lower()})

# 3. Quotation.make.SalesOrder vs convert_to_order — duplicate paths?
make_so = "Quotation.make.SalesOrder" in by
convert = [n for n in names if n.startswith("Quotation.convert_to_order")]
if make_so and convert:
    bugs.append(
        (
            "duplicate_path",
            "Quotation→SalesOrder",
            f"Both Quotation.make.SalesOrder and {convert} exist — unclear which agents should call; risk of double-order",
        )
    )

# 4. SalesOrder has invoiced_status in create but no Invoice tools
if "invoiced_status" in props("SalesOrder.create") or "invoiced_status" in props("SalesOrder.update"):
    inv = [n for n in names if "Invoice" in n or "invoice" in n.lower()]
    if not any("Invoice." in n for n in names):
        bugs.append(
            (
                "dead_end",
                "SalesOrder.invoiced_status",
                f"Field exists on SalesOrder but no Invoice entity tools (related: {inv[:5]}) — status can never leave unbilled via MCP",
            )
        )

# 5. BugReport create without update
if "BugReport.create" in by and "BugReport.update" not in by:
    bugs.append(("incomplete_crud", "BugReport", "create/list/get but no update — cannot change status/assignee after filing"))

# 6. CallNoteDraft without write-back
cnd = [n for n in names if "CallNoteDraft" in n]
if cnd and not any(x in "".join(cnd) for x in ("approve", "publish", "create", "update")):
    bugs.append(
        (
            "orphan",
            "CallNoteDraft",
            f"Only {[n.split('.',1)[1] for n in cnd]} — drafts cannot be approved or written to Note/Activity via dedicated tool",
        )
    )

# 7. CampaignAudience without Campaign
if "CampaignAudience" in ents and "Campaign" not in ents:
    bugs.append(
        (
            "orphan",
            "CampaignAudience",
            f"ops={ents['CampaignAudience']} but no Campaign entity — audience with nothing to send to",
        )
    )

# 8. Goal/Pipeline/AccountPlan read-only while Deal has pipeline field
for e in ("Goal", "Pipeline", "AccountPlan"):
    ops = set(ents.get(e, []))
    if ops and ops <= {"list", "get"}:
        bugs.append(("read_only", e, f"only {sorted(ops)} — agents cannot create/update forecast or pipeline definitions"))

# 9. Activity/Note single party
for e in ("Note", "Activity"):
    p = props(f"{e}.create")
    if "party_id" in p and "party_ids" not in p and "participants" not in p:
        bugs.append(("schema", f"{e}.create", "single party_id only — multi-attendee meetings cannot be recorded"))

# 10. Lead.make.Deal vs Lead.convert — overlap?
lead_make = [n for n in names if n.startswith("Lead.make") or "Lead.convert" in n]
print("\nLead conversion tools:", lead_make)
for n in lead_make:
    print(f"  {n} required={required(n)} props={sorted(props(n))}")

# 11. Deal mark_won from new/qualification skipping stages — allowed by API?
won = [n for n in names if "Deal.mark_won" in n]
lost = [n for n in names if "Deal.mark_lost" in n]
print("\nDeal win/loss transitions:", won + lost)

# 12. Required fields that block empty-argument list-style agent mistakes
print("\n=== LIST optional filters vs CREATE required ===")
for e in ("Lead", "Deal", "Quotation", "SalesOrder"):
    print(f"{e}.list required={required(f'{e}.list')} props={sorted(props(f'{e}.list'))[:12]}")

# 13. Quotation.send vs Deal.send_proposal — both "send"?
print("\n=== send tools ===")
for n in names:
    if "send" in n.lower() and n.split(".")[0] in ("Deal", "Quotation", "Lead", "SalesOrder"):
        print(n, "required=", required(n), "props=", sorted(props(n)))

# 14. PartyRelationship enum vs Deal referral
pr_enum = enum("PartyRelationship.create", "relationship") or []
print("\nPartyRelationship enum:", pr_enum)
if "referred_by" in pr_enum and "referral" in (enum("Deal.create", "source") or []):
    bugs.append(
        (
            "disconnected",
            "referral attribution",
            "Deal.source=referral and PartyRelationship.referred_by both exist but nothing links a Deal to the referring party",
        )
    )

# 15. SalesOrder.confirm / approval vs no invoice
print("\nSalesOrder transitions:")
for n in names:
    if n.startswith("SalesOrder.") and n not in ("SalesOrder.list", "SalesOrder.get", "SalesOrder.create", "SalesOrder.update"):
        print(n, required(n), sorted(props(n)))

# 16. Item without inventory
item_ops = ents.get("Item", [])
if item_ops and not any("stock" in o or "inventory" in o for o in item_ops):
    # check fields
    ip = props("Item.create")
    stockish = [k for k in ip if any(x in k.lower() for x in ("stock", "qty", "inventory", "warehouse"))]
    if not stockish:
        bugs.append(("gap", "Item", f"ops={item_ops}; no stock/inventory fields on Item.create ({sorted(ip)[:15]}...)"))

# 17. Company read-only but Deal/Lead require company_id?
for e in ("Lead.create", "Deal.create", "Quotation.create", "SalesOrder.create"):
    r = required(e) if e.replace(".create", "") else []
    p = props(e)
    if "company_id" in p or "company_id" in required(e):
        if "Company.create" not in by:
            bugs.append(
                (
                    "dependency",
                    e,
                    f"references company_id (required={('company_id' in required(e))}) but Company is {ents.get('Company')} — cannot create companies via MCP",
                )
            )
            break

# 18. Check Lead.create for party_id only
lp = props("Lead.create")
print("\nLead.create props:", sorted(lp))
print("Lead.create required:", required("Lead.create"))

# 19. Quotation products vs Deal products schema drift
import json as _json

def item_schema(tool, field="products"):
    p = props(tool).get(field) or {}
    items = p.get("items") or {}
    return sorted((items.get("properties") or {}).keys())

print("\n=== products line-item schema drift ===")
for t in ("Deal.create", "Quotation.create", "SalesOrder.create"):
    if t in by:
        print(t, "products keys:", item_schema(t) or item_schema(t, "items") or "N/A / check fields")
        # also line items field name
        print("  top-level array fields:", [k for k, v in props(t).items() if v.get("type") == "array"])

# 20. Dual party_id and contact_id on Deal — which wins?
if "party_id" in dp and "contact_id" in dp:
    bugs.append(
        (
            "ambiguous",
            "Deal.create",
            "both party_id and contact_id accepted — agent ambiguity which is primary contact; neither is multi-value",
        )
    )

# 21. probability not constrained 0-100?
prob = props("Deal.create").get("probability") or {}
if "minimum" not in prob and "maximum" not in prob:
    bugs.append(("validation", "Deal.create.probability", f"no min/max in schema: {prob}"))

# 22. AgentSession channel with no Channel.send
ch = enum("AgentSession.create", "channel") or []
send_tools = [n for n in names if "Channel.send" in n or n.endswith(".send") and "email" in n.lower()]
if ch and not any("Channel.send" == n or n == "Email.send" for n in names):
    bugs.append(
        (
            "dead_channel",
            "AgentSession.create.channel",
            f"enum={ch} but no Channel.send / Email.send — sessions can be labeled for channels that cannot send",
        )
    )

# Print all bugs
print("\n\n========== BUG REGISTER ==========")
by_sev = defaultdict(list)
for kind, where, detail in bugs:
    by_sev[kind].append((where, detail))

for kind, items in sorted(by_sev.items()):
    print(f"\n## {kind} ({len(items)})")
    for where, detail in items:
        print(f"- [{where}] {detail}")

print(f"\nTOTAL_FINDINGS {len(bugs)}")

# Extra: compare Deal.update vs create field parity
print("\n=== Deal.create vs Deal.update field delta ===")
c, u = set(props("Deal.create")), set(props("Deal.update"))
print("create-only", sorted(c - u))
print("update-only", sorted(u - c))

print("\n=== Quotation.create vs update delta ===")
c, u = set(props("Quotation.create")), set(props("Quotation.update"))
print("create-only", sorted(c - u))
print("update-only", sorted(u - c))

print("\n=== SalesOrder.create vs update delta ===")
c, u = set(props("SalesOrder.create")), set(props("SalesOrder.update"))
print("create-only", sorted(c - u))
print("update-only", sorted(u - c))

# Lead make.Deal required
print("\n=== Lead.make.Deal ===")
if "Lead.make.Deal" in by:
    print("required", required("Lead.make.Deal"))
    print("props", sorted(props("Lead.make.Deal")))

print("\n=== Deal.make.Quotation ===")
if "Deal.make.Quotation" in by:
    print("required", required("Deal.make.Quotation"))
    print("props", sorted(props("Deal.make.Quotation")))

print("\n=== Quotation.make.SalesOrder ===")
if "Quotation.make.SalesOrder" in by:
    print("required", required("Quotation.make.SalesOrder"))
    print("props", sorted(props("Quotation.make.SalesOrder")))
