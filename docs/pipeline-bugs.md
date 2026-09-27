# Pipeline bugs — from `tools-list.json` + live MCP probes

Evidence:
- Saved catalog: [tools-list.json](../tools-list.json) (**247** tools, captured earlier)
- **Live seat** `team06@theschoolofai.in` on Suryodaya, probed 2026-09-22: `tools/list` now returns **238** tools; raw call log in [pipeline-bugs-live.jsonl](./pipeline-bugs-live.jsonl)

```
Lead ──► Deal ──► Quotation ──► SalesOrder ──✗ Invoice/Payment (via MCP)
 │         │          │              │
 │         │          │              └─ confirm / approval.submit only
 │         │          ├─ send / accept / decline
 │         │          └─ make.SalesOrder  AND  convert_to_order.*  (both LIVE)
 │         ├─ qualify → send_proposal → negotiate → mark_won/lost
 │         └─ mark_lost.new LIVE; mark_won.new NOT in live tools/list
 └─ Lead.make.Deal + Lead.contact LIVE; Lead.convert.* NOT in live tools/list
```

---

## Live-confirmed (logged in, tools/call)

| Status | Finding | Live evidence |
|---|---|---|
| **CONFIRMED P0** | No aggregate reporting | `SalesOrder.list` → `total=310`, page size 20; `SalesOrder.aggregate` → `tool_not_available` |
| **CONFIRMED P0** | Quote→order dual path | Both `Quotation.make.SalesOrder` and `Quotation.convert_to_order.sent\|accepted.converted` are in live `tools/list` and accept calls (bogus id → `not_found`, not `tool_not_available`) |
| **CONFIRMED P0** | Invoicing dead-end for agents | `Invoice.list` / `SalesOrder.make.Invoice` → `tool_not_available`. First SO page: **27 unbilled / 3 invoiced** — invoicing happens somehow, but **not via this seat’s MCP tools** |
| **CONFIRMED P0** | Funnel linkage is broken in data | Quotation page (50): **47 without `deal_id`**, only 3 linked. SO page (30): only **5 with `quotation_id`**. Pipeline is not a connected chain in practice |
| **CONFIRMED P1** | `products` vs `items` naming | `Deal.make.Quotation` with `products` → `products is not an accepted argument`. Same call with `items` → advances to `rate is required` — field rename is real |
| **CONFIRMED P1** | Relationship graph unused | `PartyRelationship.list` → **`total=0`**. Meanwhile Deal page has **6 `source=referral`** with nowhere to attach a referrer |
| **CONFIRMED P1** | Company cannot be created | `Company.create` → `tool_not_available`; `Company.list` → **1** company only |
| **CONFIRMED P1** | Deals stall in `new` | Deal page (50): **34 `new`**, 12 `closed_lost`, 3 `closed_won`, 1 `qualification` — almost no mid-funnel motion |
| **CONFIRMED P1** | Ambiguous party vs contact | **7/50** deals have `party_id ≠ contact_id` |
| **STALE in saved JSON** | `Lead.convert.*` / `Deal.mark_won.new` | In saved `tools-list.json` but **absent from live `tools/list` (238)** and calls return `tool_not_available`. Do not train agents on the old 247 dump blindly |
| **ASYMMETRY** | Lose-from-new allowed, win-from-new not | `Deal.mark_lost.new.closed_lost` is LIVE; `Deal.mark_won.new.closed_won` is not — stage-skip still exists, but only for losses |
| **IMPROVED** | Error messages name the field | `SalesOrder.get {}` / `Deal.get {}` → `errors: [{field: "/id", message: "id is required."}]` — better than the old root-path-only claim |

---

## P0 — breaks agent correctness or revenue chain

| ID | Bug | Catalog evidence | Why it bites an agent |
|---|---|---|---|
| **P0-1** | Quote→order has **two parallel tools** | `Quotation.make.SalesOrder` (id + optional order fields) **and** `Quotation.convert_to_order.{accepted,sent,viewed}.converted` (id only) — **both live** | Agent may call both → duplicate orders, or pick the wrong one after a stage change |
| **P0-2** | Order can be created **without acceptance** | `Quotation.convert_to_order.sent.converted` and `.viewed.converted` exist alongside `.accepted.converted` — **sent path live** | Funnel integrity is optional; “sent” quote can become a SalesOrder |
| **P0-3** | Revenue chain **dead-ends** at SalesOrder for MCP | Live: orders show `invoiced` and `unbilled`, but `Invoice.*` unavailable to seat | Agent cannot bill; UI/admin path may exist outside MCP |
| **P0-4** | Lead “convert” tools gone live; only `make.Deal` | Saved JSON had `Lead.convert.*`; live list does not. `Lead.make.Deal` remains | Agents using stale catalogs will call missing convert tools |
| **P0-5** | Stage-skip asymmetric | `Deal.mark_lost.new.closed_lost` **live**; `Deal.mark_won.new` **not** | Agents can still jump `new` → `closed_lost` without mid stages |
| **P0-6** | Orphan quotations / orders | Live: **94%** of first 50 quotes have no `deal_id`; few SOs reference a quotation | Reporting “pipeline conversion” from Deal→Quote→Order will undercount badly |
## P1 — schema / naming bugs that cause wrong tool arguments

| ID | Bug | Catalog evidence | Why it bites an agent |
|---|---|---|---|
| **P1-1** | Line items renamed mid-pipeline | `Deal.create` / `Lead.make.Deal` use **`products[]`**; `Quotation.create` / `Deal.make.Quotation` / `SalesOrder.create` use **`items[]`** | Same concept, different key — copy-forward from Deal → Quotation fails schema validation |
| **P1-2** | Line-item shape widens without a mapper | Deal `products` keys: `item_id, qty, rate, discount, description, amount`. Quotation/SalesOrder `items` add full GST/TDS/CESS split (`hsn_or_sac`, `cgst_*`, `sgst_*`, `igst_*`, `tds_*`, …) | Agent must invent tax rows; no “promote products → items” helper |
| **P1-3** | Dual contact fields, both scalar | `Deal.create` required: `title`, `party_id`. Also optional `contact_id`. Both are strings, not arrays | Ambiguous primary person; buying committee impossible |
| **P1-4** | `source=referral` with no referrer | `Deal.create.source` / `Lead.create.source` include `referral`; `PartyRelationship.relationship` includes `referred_by` / `referred`; **no** `referred_by_party_id` on Deal/Lead | Referral is a label, not a link — graph and deal stay disconnected |
| **P1-5** | Source enum drift Lead vs Deal | Lead: `website, referral, campaign, cold_call, social_media`. Deal adds `partner`, `existing_customer` | `Lead.make.Deal` can set Deal sources the Lead never had — or drop values on reverse reasoning |
| **P1-6** | Item HSN field duplicated | `Item.create` has both `hsn_or_sac` and `hsn_sac` | Agents write one, reports read the other → silent GST gaps on line items |
| **P1-7** | `probability` unconstrained | `Deal.create.probability`: `{type: number, default: 0}` — no min/max | Values like `150` or `-1` are schema-legal |
| **P1-8** | Company is read-only but required downstream | `Company`: `list`/`get` only. `Quotation.create` / `SalesOrder.create` **require** `company_id` | Agent cannot onboard a new company; stuck if seed data missing |

---

## P2 — supporting CRM surface that looks like pipeline but is broken

| ID | Bug | Catalog evidence |
|---|---|---|
| **P2-1** | Interactions are single-party | `Note.create` / `Activity.create`: one `party_id` — no participants |
| **P2-2** | `CallNoteDraft` is a cul-de-sac | `list`/`get` only — no approve/publish into `Note`/`Activity` |
| **P2-3** | `CampaignAudience` without `Campaign` | Audience CRUD exists; no campaign/send entity |
| **P2-4** | `Goal` / `Pipeline` / `AccountPlan` read-only | Deal has `pipeline` string field; cannot manage pipeline definitions via MCP |
| **P2-5** | Channels declared, none sendable | `AgentSession.create.channel` ∈ `{web,whatsapp,telegram,slack,discord,email,api}` but no `Email.send` / `Channel.send` (only `endpoint.email.public.subscriber_lists`) |
| **P2-6** | `BugReport` create-only | `create`/`list`/`get` — no `update` (cannot close or assign) |

---

## Intended happy path (what an agent *should* call)

If the stage machine were enforced, the catalog implies:

1. `Lead.create` → `Lead.contact` → `Lead.qualify.*` → **`Lead.make.Deal`** (not bare `convert.*`)
2. `Deal.qualify` → `Deal.send_proposal` → `Deal.negotiate` → `Deal.make.Quotation`
3. `Quotation.send` → `Quotation.accept.*` → **`Quotation.make.SalesOrder`** *or* `Quotation.convert_to_order.accepted.converted` (pick one; today both exist)
4. `SalesOrder.confirm` / `SalesOrder.approval.submit` → **stop** (no invoice tool)

Anything using `*.mark_won.new.*`, `Lead.convert.new.*`, or `convert_to_order.sent|viewed` is a **bypass**, not a bug in the agent.

---

## Suggested fixes (platform)

1. **Deprecate or gate** `Quotation.convert_to_order.sent|viewed` — only `.accepted` (or fold into `make.SalesOrder` with a stage check).
2. **Rename or document** `Lead.convert.*` as status-only; require `Lead.make.Deal` for commercial conversion (or make `convert` call `make.Deal` atomically).
3. **Remove or permission-gate** `Deal.mark_won.new` / `mark_lost.new` (and possibly qualification/proposal shortcuts).
4. **Unify** line items: one field name (`items`) and a shared schema from Deal onward; auto-map on `Deal.make.Quotation`.
5. **Add** `Invoice.*` + `SalesOrder.make.Invoice` (or stop exposing `invoiced_status` as writable fiction).
6. **Add** `referred_by_party_id` on Deal/Lead when `source=referral`; drop duplicate `hsn_sac`; constrain `probability` to `0..100`.
7. **Add** `Company.create` or stop requiring `company_id` on Quotation/SalesOrder create when inherited from Deal.

---

*Generated from static schema inspection of `tools-list.json`. Behavioural enforcement (whether the server rejects illegal transitions at runtime) is not proven here — several tools are *advertised* that skip stages, which is enough for agents to mis-orchestrate.*
