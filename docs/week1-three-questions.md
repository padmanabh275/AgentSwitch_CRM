# Week 1 deliverable — three questions

| | |
|---|---|
| **Seat** | `team06@theschoolofai.in` · roles `sales_user`, `user`, `agent_user` · apps `crm`, `agent` |
| **Evidence** | `tools/list` → **247 tools / 51 entities** · live `SalesOrder.list` (`total: 310`, `limit: 20`) · rechecked against [tools-list.json](../tools-list.json) 2026-09-22 |
| **Compared against** | Salesforce Agentforce · HubSpot Breeze · Attio · Zoho CRM · ERPNext |
| **Full register** | [gap-analysis.md](./gap-analysis.md) · [AgentSwitch-Gap-Report.docx](./AgentSwitch-Gap-Report.docx) |

One page. Three questions. Specifics only. Tool names below are exact catalog strings.

---

## 1. What do they do that we do not?

Concrete absences in the CRM/agent surface this seat can see — not vibes.

| Gap | Evidence in our MCP surface | Who ships it |
|---|---|---|
| No aggregate / group-by reporting | No `*.aggregate` tool in the 247. `SalesOrder.list` returns 20 of 310; *"revenue this quarter by owner"* is 16 round trips plus client-side sum | Zoho reports, Agentforce + Data Cloud, ERPNext |
| No CRM invoice, payment, or credit note | Quote → order exists (`Quotation.make.SalesOrder`, also `Quotation.convert_to_order.*`). Live orders sit at `invoiced_status: "unbilled"`. No `Invoice.*` / `CreditNote.*` / `SalesOrder.make.Invoice`. (Storefront has `endpoint.storefront.payment.verify` — commerce checkout, not AR billing.) | Zoho, Odoo, ERPNext |
| GST fields with no filing path | Order carries `gst_treatment`, `place_of_supply`, `gst_no`, `tds_*`, `tcs_*`; line items carry `hsn_or_sac`. No IRN, QR, or GSTR export tool | ERPNext native e-invoice |
| Agents can be run, not authored | `AgentSkill`, `AgentPersona`, `AgentRunbook`, `AgentToolPolicy`, `AgentProvider`, `AgentFloorConfig`, `AgentJob` are `list`/`get` only (`AgentRunbookRun` likewise). `AgentPersona.daily_limits` is a separate read-only route | Agentforce Builder, Breeze Studio |
| No knowledge grounding / RAG | `FileAttachment` is create/list/get/update of blobs; no `Knowledge.ingest` / `Knowledge.search` | Agentforce Intelligent Context, Copilot Studio |
| No service desk or SLA clock | `BugReport`: `create` / `list` / `get` only — no `update`, no queue, no SLA, no CSAT. No `Ticket.*` | HubSpot Service Hub, Zoho Desk |
| No outbound email, sequences, or templates | Sole email tool: `endpoint.email.public.subscriber_lists` (read). No `Email.send`, no `Sequence.*` | Attio Pro, HubSpot, Zoho |
| No forecast, quota, or territory rollup | `Goal`, `Pipeline`, `AccountPlan` are `list`/`get` only. `Deal.create.probability` is a typed number (default 0), not a model output | Zoho Enterprise, Clari |
| No dedupe, merge, or enrichment | `Party`: create/list/get/update. No `Party.find_duplicates`, `Party.merge`, or enrichment resolve | Clay, Breeze Intelligence, Attio |
| Interactions name only one person | `Note.create` and `Activity.create` each take a single `party_id` (plus optional `deal_id` / `lead_id` / `company_id`). No participant array | Every calendar-integrated CRM |
| Deals are single-threaded | `Deal.create` takes `products` as an **array** but `contact_id` and `party_id` as **scalars**. `source` enum includes `referral` with no referrer party field | Salesforce opportunity contact roles, HubSpot deal contacts |
| Relationship edges exist but are inert | `PartyRelationship` has create/list/get/update over **16** typed edges (`guardian` … `counterparty`, including `referred_by` / `referred`). Nothing observes, proposes, or walks them — and there is no `delete` | Nobody holds *observed* graph; TeamLink is outside CRM |

---

## 2. Which of those can an agent close with tools this seat already has?

Two buckets. Do not confuse them.

### Ours to fix (new tables / endpoints — agent cannot invent these)

| Missing capability | Why the seat cannot close it by orchestration |
|---|---|
| `{Entity}.aggregate` | No group-by tool exists; paginating 310 orders and summing in the model is not reporting |
| `Invoice` / `CreditNote` + `SalesOrder.make.Invoice` | Revenue chain dead-ends at SalesOrder; GST filing has nowhere to hang. Storefront `payment.verify` does not bill a sales order |
| `AgentSkill.create` / `AgentPersona.create` / version / publish | Governance entities are read-only; an agent cannot author what it cannot write |
| `Knowledge.ingest` / `Knowledge.search` | Attachments are unindexed; no retrieval surface |
| `Ticket.*` with SLA | `BugReport` has no update path and no queue semantics |
| `Email.send` / `Sequence.*` | No send tool on any channel despite `AgentSession.create.channel` ∈ `{web, whatsapp, telegram, slack, discord, email, api}` |
| `ActivityParticipant` join | Schema forbids multi-party interactions; co-attendance cannot be expressed |
| `DealContact` + `referred_by_party_id` | Buying committee and referral attribution are not columns an agent can invent |

### Yours to build (orchestration over what already exists)

| Agent goal | Tools this seat already has (exact names) | What the agent must do |
|---|---|---|
| Move a lead → deal → quote → order | `Lead.qualify.*` / `Lead.make.Deal` → `Deal.qualify` → `Deal.send_proposal` → `Deal.negotiate` → `Deal.mark_won.*` / `Deal.mark_lost.*` → `Deal.make.Quotation` → `Quotation.send` / `Quotation.accept.*` → `Quotation.make.SalesOrder` (or `Quotation.convert_to_order.*`) → `SalesOrder.confirm` / `SalesOrder.approval.submit` | Hold the stage goal, re-`get` after each transition, branch on failure |
| File notes and activities on a party/deal | `Note.create` / `.update`; `Activity.create` / `.update`; `CallNoteDraft.list` / `.get` (read-only drafts — **no approve/publish tool**) | Draft in CallNoteDraft if present, then write `Note` / `Activity` yourself; escalate if human edit is required |
| Respect spend and reach caps | `AgentPersona.daily_limits` (tokens/USD used vs budget, blocked flag, reset 00:00 UTC); `AgentToolPolicy.list` / `.get`; `AgentFloorConfig.list` / `.get` | Refuse or escalate when limits are exhausted before calling spendy tools |
| Escalate when stuck | `AgentEscalation.create` / `.list` / `.get` | Park the goal, hand a human a decision, resume after resolution |
| Prove what the agent did | `endpoint.job_ledger.verify` / `endpoint.job_ledger.replay` / `endpoint.job_ledger.forensics`; `AgentLedgerSeal.list` / `.get` | After a multi-step run, produce a replayable audit trail |
| Honor DSAR / retention | `PrivacyRequest.create` / `.update`; `endpoint.agent_governance.privacy.forget.preview`; `AgentRetentionRule.list`; `LedgerRetentionPolicy.list`; `endpoint.job_ledger.retention.preview` | Preview blast radius, then execute forget under policy |
| Operate the agent floor | `endpoint.mission_control.staffing_forecast` / `.incidents` / `.incident_timeline` / `.runbooks`; `AgentRunbook.list` / `AgentRunbookRun.list` | Treat agents as a staffed floor, not toggles |
| Keep working memory across steps | `AgentMemory.create` / `.list`; `AgentTodo.create` / `.list`; `AgentTask.create` / `.list` | Persist intermediate state so a twenty-step goal survives session boundaries |
| Manual relationship hygiene | `PartyRelationship.create` / `.update` / `.list` / `.get`; `Party.list` / `.get` | Type declared edges (`referred_by`, `employer`, `billing_contact`, …) — **not** observed inference |

If a gap needs a column that is not there, stop and file it under *ours*. If every step already has a tool, that is *yours* — write the skill / runbook, do not wait for platform.

---

## 3. What can an agent do that their product cannot?

Their product is a UI over a database: a human clicks every step. Our seat is a **permissioned tool surface plus a job ledger**. That changes what “using the CRM” means.

| Their UI forces | An AgentSwitch agent can |
|---|---|
| One screen, one action, one human | Hold a goal across twenty tool calls — lead qualify → deal negotiate → quotation → sales order confirm — re-reading `Deal.get` / `Quotation.get` / `SalesOrder.get` when state changes underneath |
| Telemetry dashboards after the fact | Prove a specific action with `endpoint.job_ledger.replay` / `.forensics` and `AgentLedgerSeal` — cryptographic answer to *"what did the agent do and why"* |
| Soft policy documents | Read `AgentToolPolicy` + `AgentPersona.daily_limits` + `AgentFloorConfig` **before** execution; hard-stop or escalate when `blocked` |
| Client-side “are you sure?” on writes | Server-side caller-scoped discovery (`_meta.agentswitch.discovery: "caller"`) — `tools/list` only returns what this seat’s roles allow |
| Privacy as a ticket queue | Runtime DSAR: `PrivacyRequest` + `endpoint.agent_governance.privacy.forget.preview` with blast radius before delete |
| Pipeline fields a human typed (`probability`, stage) | *(Opening, not yet built)* Walk an **observed** relationship graph at deal time — buying committee, warm path, champion decay — with every edge traceable via the ledger. Schema seed exists (`PartyRelationship`, 16 types); capture and traversal are still ours to ship |

**The line to hold in a buyer conversation:** competitors sell *pipeline* intelligence (attributes of a record). We are built to sell *agent* intelligence (a goal held across mutating state, under policy, with proof). Relationship intelligence on deals is the product differentiator to build next; governance + ledger endpoints are the differentiators already shipped and under-marketed.

---

*Rechecked 2026-09-22 against `tools-list.json` (247 tools). Corrections from prior draft: ledger/mission-control tools are `endpoint.job_ledger.*` / `endpoint.mission_control.*`; forget preview is `endpoint.agent_governance.privacy.forget.preview`; `AgentPersona.daily_limits` is its own tool; `CallNoteDraft` has no approve path; `hsn_or_sac` lives on line items; storefront `payment.verify` is not CRM AR.*
