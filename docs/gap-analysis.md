


AgentSwitch

CRM and Multiagent Platform

COMPETITIVE GAP REPORT

What the MCP catalog exposes today, where it trails the platforms it will be compared against, and what to build next.

| Instance | Suryodaya (India) — https://agentswitch.theschoolofai.in |
| --- | --- |
| Evidence captured | 2026-09-17 via initialize, tools/list and live tools/call probes |
| Raw data | tools-list.json · sales-order-list.json |
| Caller scope | team06@theschoolofai.in — roles sales_user, user, agent_user; apps crm, agent |
| Interactive version | ~/.cursor/projects/c-Users-Padmanabh-OneDrive-Documents-Agent-Switch/canvases/gap-report.canvas.tsx |
| Delivery window | 3 weeks · 2–3 engineers · 13 of 30 gaps addressed — see section 7 |



> Week-1 one-pager (three questions): [week1-three-questions.md](./week1-three-questions.md) · [Week1-Three-Questions.docx](./Week1-Three-Questions.docx)


## 1. Executive summary

### At a glance

| Metric | Value |
| --- | --- |
| MCP tools exposed | 247 |
| Entities | 51 |
| Entities that are read-only | 25 (49%) |
| Entities supporting delete | 6 |
| Business domains with no entity at all | 6 |
| Gaps identified | 30 |
| Closed in the 3-week plan | 13 (6 of 8 P0) |
| Deferred, with reasons | 17 |


### Three findings

1. Half the platform is read-only, and it is the half you sell. 25 of 51 entities expose only list and get. They cluster in exactly the agent-configuration surfaces a multiagent platform gets bought for — AgentSkill, AgentPersona, AgentRunbook, AgentToolPolicy, AgentProvider, AgentFloorConfig, AgentJob. An agent can be operated through MCP but not built, versioned, tested or deployed. That is precisely the ground Agentforce Builder and Breeze Studio compete on.

2. No agent can ask an aggregate question. SalesOrder.list reports total: 310 at limit: 20. Answering "what did we sell this quarter" costs sixteen round trips of raw rows that the model must then re-add itself. Every competitor with a reporting engine answers it in one call.

3. Deals are single-threaded by schema — and that is the opening. Deal.create accepts products as an array but contact_id as a scalar. The deal knows every line item and exactly one human. Meanwhile PartyRelationship is a real typed, directed, time-bounded edge table with full create and update that nothing populates and nothing traverses. Closing that loop turns relationship intelligence into a deal-level differentiator on ground no competitor holds. See section 5.

### Recommendation

Findings 1 and 2 are fixable without touching the data model — they are surface-level additions to an otherwise sound catalog, which is what makes a three-week window worth taking seriously at all.

The plan in section 7 commits 35 of 45 available engineer-days to 13 of the 30 gaps, chosen on one principle: the P0 gaps are horizontal. A single generic implementation at the shared CRUD layer lands aggregation, bulk writes, structured errors and progressive tool disclosure across all 51 entities and all 247 tools simultaneously. Five gaps close that way for 12 engineer-days, and a sixth (P0-8) is a small schema change — six of the eight P0 gaps, for barely a quarter of the sprint. Breadth here is a property of the architecture, not of heroics.

The rest of the capacity goes to relationship intelligence on deals (6.2) rather than to more parity work, deliberately. It is the only item in this report that is both unbuilt and unclaimed by any competitor; its prerequisite (P0-8) is among the cheapest changes in the register; and a walkable graph is demonstrable inside three weeks where invoicing and agent authoring are not. Separately, the governance and ledger capabilities in 6.1 need no engineering at all — they are already shipped and simply not being talked about. That is the cheapest win available and belongs in these same three weeks as a positioning task, not a build task.

What three weeks cannot buy is named explicitly in 7.4. The quote-to-cash chain, agent authoring, ticketing and knowledge grounding are quarter-scale surfaces; GST filing, outbound email and OAuth are gated on third parties. Those set the agenda for weeks 4 through 16 and should not be promised inside this one.


## 2. What the catalog contains

### 2.1 Tool shapes

| Shape | Count | Share |
| --- | --- | --- |
| CRUD tools (list / get / create / update / delete) | 159 | 64% |
| REST passthroughs (endpoint.*) | 44 | 18% |
| State transitions (permission: submit) | 37 | 15% |
| Composite and document-chain tools | 7 | 3% |
| Total | 247 |  |


Across the 51 entities:

26 accept create; 25 are read-only.

Only 6 expose delete: AddressBook, AddressBookEntry, ContactGroup, ContactGroupMember, TrustedSender, UserPreference.

Transitions cluster on the sales funnel — Deal 11, Lead 9, Quotation 8, AgentSession 4, AgentTask 4, SalesOrder 1.

BugReport is the anomaly: create with no update.

### 2.2 Write coverage by domain

| Domain | Read + write | Read-only | Writable |
| --- | --- | --- | --- |
| CRM core | 11 | 4 | 73% |
| Contacts and platform | 9 | 4 | 69% |
| Agent runtime | 4 | 4 | 50% |
| Agent governance | 1 | 7 | 13% |
| Compliance and ledger | 1 | 6 | 14% |


The two lowest rows are the finding. Governance and compliance are almost entirely observable and almost entirely unconfigurable through MCP.

### 2.3 Domains with no entity at all

| Missing domain | Nearest thing we have |
| --- | --- |
| Invoicing and payments | SalesOrder.invoiced_status — never written |
| Service desk and SLA | BugReport — create-only |
| Marketing execution | CampaignAudience — no campaign, no send |
| Reporting and dashboards | Per-entity list with limit 20 |
| Knowledge and grounding | FileAttachment — unindexed |
| Inventory and purchasing | Item — no stock, no vendor side |



## 3. Competitive landscape

AgentSwitch will be measured against three different sets of expectations depending on who is in the room.

### 3.1 The three tiers

Enterprise agentic CRM

Salesforce Agentforce 360 — Agentforce Builder, Agent Script for deterministic control, Agent Health Monitoring, Data Cloud grounding; 18,500 customers and 3 billion monthly workflows. HubSpot Breeze — Copilot, Agents, Intelligence and Studio, shipped down to the free CRM tier. Microsoft Dynamics 365 Copilot and Copilot Studio — Graph grounding, computer use, Power Platform governance.

AI-native MCP CRM

Attio — hosted server at mcp.attio.com/mcp, OAuth, reads auto-approve and writes require confirmation. Close — the other CRM with first-party MCP. Day.ai — captures first and structures later, exposes customer memory over MCP. Twenty — open source, self-hostable, permission-aware agents. Plus folk, and Clay as an enrichment layer rather than a CRM.

India SMB CRM and ERP

Zoho CRM — CPQ, forecasting, inventory and Zia, with AI agents from the Professional tier. Odoo. ERPNext and Frappe — GST and e-invoicing native. Vtiger, Kylas, LeadSquared.

### 3.2 Capability matrix

Assessed against one representative per tier. Y = ships it, ~ = partial, · = absent.

| Capability | AgentSwitch | Agentforce | Breeze | Attio | Zoho | ERPNext |
| --- | --- | --- | --- | --- | --- | --- |
| First-party MCP server | Y | ~ | · | Y | · | · |
| Permission-scoped tool discovery | Y | ~ | · | ~ | · | · |
| Cryptographic run ledger | Y | · | · | · | · | · |
| DSAR and retention automation | Y | ~ | ~ | · | ~ | · |
| Human escalation queue | Y | Y | Y | · | ~ | · |
| Per-agent cost and token budgets | Y | ~ | ~ | · | · | · |
| Agent authoring and deploy | · | Y | Y | ~ | Y | · |
| Agent evaluation and testing | · | Y | ~ | · | · | · |
| Knowledge grounding and RAG | · | Y | Y | ~ | Y | · |
| Aggregate reporting | · | Y | Y | ~ | Y | Y |
| Forecasting and quota | · | Y | Y | · | Y | ~ |
| Invoicing and payments | · | ~ | ~ | · | Y | Y |
| India GST e-invoicing | · | · | · | · | ~ | Y |
| Service desk and SLA | · | Y | Y | · | Y | ~ |
| Email sequences | · | Y | Y | Y | Y | · |
| Enrichment and dedupe | · | Y | Y | Y | Y | · |
| Voice and telephony | · | Y | ~ | · | Y | · |
| Observed relationship graph | · | · | · | ~ | · | · |


The shape of that table is the strategy. We win the top six rows outright and lose the middle eleven — the top six are governance and trust, the middle eleven are table stakes. The last row is different from both: nobody holds it. That is the one to take.

### 3.3 Commercial models

| Vendor | Model |
| --- | --- |
| Agentforce | Enterprise tier plus consumption; Data Cloud setup measured in weeks to months |
| Breeze | Credits at USD 10 per 1,000 — a resolved conversation costs 50 credits, about USD 0.50 |
| Copilot Studio | USD 200/month per 25,000 credits; Dynamics SKUs include Copilot as of 2026 |
| Attio | Per seat, annual only; free tier of 3 seats and 50,000 records |
| Zoho CRM | Free for 3 users; INR 800 – 2,400 per user per month |
| Kylas | INR 12,999/month flat, unlimited users, up to 100,000 records |
| ERPNext | No licence fee; roughly INR 3–6 lakh per year all-in for 25 users |


The India SMB tier anchors buyer expectations far below the enterprise agentic tier. That is the corridor AgentSwitch has to price into while carrying governance that looks like the top tier — which is only defensible if the governance story is told loudly.


## 4. Gap register

Priority — P0 blocks agent usefulness or a live deal · P1 is parity a competitor markets against us · P2 extends a defensible advantage.

Effort — S under a week · M one to four weeks · L a quarter-scale workstream. These size the full surface described in each row.

Wk — which week of the three-week plan the gap lands in. — means deferred, with the reason given in 7.4. Rows marked v0 ship a deliberate first cut rather than the full surface, and the part left behind is named in 7.3.

| Priority | Gaps | S | M | L | In sprint |
| --- | --- | --- | --- | --- | --- |
| P0 | 8 | 1 | 5 | 2 | 6 |
| P1 | 18 | 0 | 13 | 5 | 6 |
| P2 | 4 | 1 | 2 | 1 | 1 |
| Total | 30 | 2 | 20 | 8 | 13 |


Note the shape of that last column: the sprint closes three quarters of P0 but only a third of P1. That is the correct bias. P0 gaps block an agent from functioning at all, while P1 gaps are things a competitor can point at in a bake-off — painful, but survivable for a quarter.

### 4.1 P0 — blocks agent usefulness

| ID | Gap | Evidence | Effort | Wk | Proposed surface | Who ships it |
| --- | --- | --- | --- | --- | --- | --- |
| P0-1 | No aggregate or group-by tool | SalesOrder.list returns total 310 at limit 20; totalling revenue costs 16 round trips | M | 1 | {Entity}.aggregate(group_by, metrics, filters) plus a read-only query view | Zoho reports, Agentforce + Data Cloud |
| P0-2 | No cross-entity search | Each .list takes a local search string; nothing spans Lead, Deal, Party, Note, Activity | M | 2 | search.global(query, entities[], limit) | Attio MCP, Day.ai |
| P0-3 | All 247 schemas load upfront | initialize advertises only tools with listChanged: false — no catalog, describe or tool search | M | 3 | tools/catalog + tools/describe per SEP-2636, plus search_tools | MCP 2026-07-28 guidance, Bedrock AgentCore |
| P0-4 | Errors do not name the missing field | SalesOrder.get with {} returns -32602 and errors: [{path: "/", keyword: "required"}] | S | 1 | Structured errors carrying missing[] and a human-readable message | Baseline for agent-facing tools |
| P0-5 | No bulk or batch writes | Every create and update is single-record; re-owning 300 leads means 300 calls | M | 2 | {Entity}.bulk_create / bulk_update, plus a batch envelope | Salesforce Bulk API, Zoho, HubSpot |
| P0-6 | Revenue chain dead-ends at SalesOrder | Quotation.make.SalesOrder exists, but every order reads invoiced_status: "unbilled" with nothing to bill it | L | — | Invoice.*, Payment.*, CreditNote.*, SalesOrder.make.Invoice | Zoho, Odoo, ERPNext |
| P0-7 | Agents can be run but not authored | AgentSkill, AgentPersona, AgentRunbook, AgentToolPolicy, AgentProvider, AgentFloorConfig are list/get only | L | — | AgentSkill.create/update/version/publish, AgentPersona.create/update | Agentforce Builder, Breeze Studio, Twenty |
| P0-8 | Interactions can only name one person | Note.create and Activity.create each take a single party_id — a four-attendee meeting cannot be recorded as one | M | 1 | ActivityParticipant(activity_id, party_id, role) join table | Every calendar-integrated CRM |


### 4.2 P1 — parity gaps competitors market against us

| ID | Gap | Evidence | Effort | Wk | Proposed surface | Who ships it |
| --- | --- | --- | --- | --- | --- | --- |
| P1-1 | GST fields with no filing path | Orders carry gst_treatment, place_of_supply, hsn_or_sac, tds_*, tcs_* — no e-invoice, IRN or GSTR export | M | — | Invoice.einvoice_generate (IRN + QR), tax.gstr_export | ERPNext natively |
| P1-2 | No service desk or SLA | BugReport is create-only; no queue, no SLA clock | L | — | Ticket.* with SLA policy, queue assignment, CSAT | HubSpot Service Hub, Zoho Desk, Freshdesk |
| P1-3 | No outbound email, sequences or templates | Only endpoint.email.public.subscriber_lists, a read of public lists | L | — | Email.send, EmailTemplate.*, Sequence.* with open and reply tracking | Attio Pro, Zoho, HubSpot |
| P1-4 | Audiences with no campaign behind them | CampaignAudience previews and syncs, but no Campaign entity, send or attribution | M | — | Campaign.* with channel send and revenue attribution | HubSpot, Zoho Campaigns, Marketing Cloud |
| P1-5 | Channels declared but not callable | AgentSession.create accepts whatsapp, telegram, slack, discord, email — no tool sends on any | M | — | Channel.send(session_id, channel, payload) and inbound webhook intake | Kylas WhatsApp, Agentforce Voice |
| P1-6 | Calendar is read-only and birthday-scoped | endpoint.calendar.birthdays is the only calendar surface | M | — | Meeting.* with availability lookup and booking | Zoho booking, Attio meeting intelligence |
| P1-7 | Call intelligence stops at draft notes | CallNoteDraft drafts and approves; no call log, recording, transcript or talk-time | L | — | Call.* with transcript storage and extraction into Activity | Gong, Attio, Day.ai, Close |
| P1-8 | Agent memory has no semantic retrieval | AgentMemory.list filters on category, importance, source — no similarity search | M | 2 v0 | AgentMemory.search(query, top_k, threshold) over embeddings | Day.ai memory, Data Cloud grounding |
| P1-9 | No knowledge base or grounding store | FileAttachment stores files; nothing chunks, indexes or retrieves them | L | — | Knowledge.ingest / Knowledge.search with citation spans | Agentforce Intelligent Context, Copilot Studio |
| P1-10 | No evaluation or regression harness | skill_sandbox runs a skill ad hoc; no golden datasets, scored runs or pre-deploy gates | M | — | Eval.dataset, Eval.run, Eval.compare wired into skill publish | Agentforce Testing Center |
| P1-11 | Goals and pipelines cannot roll up | Goal and Pipeline are read-only; no forecast category, quota or territory | M | — | Forecast.rollup(period, owner_tree), Quota.*, Territory.* | Zoho Enterprise forecasting, Clari |
| P1-12 | No scoring or next-best-action | Lead.value and Deal.probability are stored inputs, not model outputs | M | — | Lead.score, Deal.risk, NextAction.suggest with explanation | Zia, Freddy 0–99 scoring, Breeze |
| P1-13 | No dedupe, merge or enrichment | Party.create has no duplicate guard; nothing enriches from an external source | M | 1 v0 | Party.find_duplicates, Party.merge, Enrichment.resolve | Clay, Breeze Intelligence, Attio |
| P1-14 | Relationship edges are declared, never observed | PartyRelationship has full CRUD over 16 typed edges, but nothing derives one from mentions or co-attendance | L | 2 v0 | PartyMention capture, rollup to proposed edges with origin, strength, confidence | Day.ai, Attio research assistant — nobody as a graph |
| P1-15 | The edge table cannot be traversed | PartyRelationship.list filters from_party_id or to_party_id exactly: one hop, one direction | M | 3 v0 | Relationship.neighbors, .path, .subgraph, .influence(deal_id) | LinkedIn Sales Navigator TeamLink |
| P1-16 | Password login issues the bearer token | /api/auth/login trades email and password for a 43-character bearer; no OAuth or scoped consent | M | — | OAuth 2.1 with per-scope consent and write-confirmation hints | Attio ships OAuth + write confirmation |
| P1-17 | No change subscriptions | Agents must poll; no webhook, resource subscription or listChanged notification | M | 2 v0 | Webhook.subscribe plus MCP resources with subscribe support | Vtiger, HubSpot events, Platform Events |
| P1-18 | Deals cannot record a buying committee | Deal.create takes products as an array but contact_id as a scalar; Deal.source can say referral with no field for who referred | M | 1 | DealContact(deal_id, party_id, role, influence) and referred_by_party_id | Salesforce opportunity contact roles, HubSpot deal contacts |


### 4.3 P2 — extend a defensible advantage

| ID | Gap | Evidence | Effort | Wk | Proposed surface | Note |
| --- | --- | --- | --- | --- | --- | --- |
| P2-1 | Long work has no job handle | mission_control.staffing_forecast and job_ledger.replay run inline with no task id or progress | M | — | Return a task id with poll and cancel; adopt MCP tasks | MCP 2026 multi round-trip guidance |
| P2-2 | Ledger proof is not a customer-facing artifact | verify, replay and forensics stop at API responses; no signed, shareable audit pack | M | — | Ledger.export_audit_pack(period) with detached signature | Nobody in the comparison set ships this |
| P2-3 | Tool policy is data, not a published contract | AgentToolPolicy and AgentFloorConfig are read-only records with no authored, versioned rule language | L | — | Versioned policy DSL with simulate and diff before rollout | Answers Agentforce Agent Script |
| P2-4 | Budget telemetry stops at today | AgentPersona.daily_limits reports today's tokens and USD against limits, resetting 00:00 UTC, with no history | S | 1 | Cost.timeseries(persona, range) with threshold alerts | Agent Health Monitoring, Copilot credits |



## 5. Spotlight: the relationship graph

Every CRM models the same funnel — lead, opportunity, order. What none of them model well is the network of people behind it.

AgentSwitch already has the edge table. PartyRelationship carries from_party_id, to_party_id, a 16-value typed relationship, is_primary, since and until, with full create and update. It is a graph schema that nothing populates and nothing walks.

The structural blocker comes first. Note.create and Activity.create each accept a single party_id. A meeting with four attendees cannot be recorded as a meeting with four attendees, so co-occurrence — the richest signal for who knows whom — is not merely unused, it is impossible to express. No amount of inference works around this. ActivityParticipant is the unlock, and it is the smallest change in the whole register.

### 5.1 Pipeline: from chat mention to traversable edge

Capture  →  AgentMessage / Note / CallNoteDraft  →  Activity + ActivityParticipant  →  Resolve mention to party_id  →  Co-occurrence signal  →  PartyMention evidence  →  Rollup: evidence_count, strength, confidence  →  Proposed edge  →  Human confirm or reject  →  PartyRelationship, origin = observed  →  neighbors / path / subgraph / influence

Figure 1. Mention-to-edge pipeline.

| Stage | What exists today | What has to be built |
| --- | --- | --- |
| 1. Capture | Note and Activity each carry one party_id; AgentMessage holds the chat text | ActivityParticipant(activity_id, party_id, role: organizer \| attendee \| cc \| mentioned) |
| 2. Resolve | Party.list matches on name, email and phone | PartyMention(source_type, source_id, party_id, span, confidence) — written per mention, not per edge |
| 3. Roll up | Nothing; every edge today is typed by hand | Threshold on repeated co-mention and co-attendance proposes an edge with evidence_count and strength |
| 4. Confirm | AgentEscalation already routes agent decisions to a human queue | Proposed edges confirmed or rejected before they count; rejection is itself training signal |
| 5. Traverse | PartyRelationship.list — one hop, one direction, exact match | neighbors(depth, min_strength, types[]), path(from, to, max_hops), subgraph(seeds[]), influence(deal_id) |


A single mention is noise, so mentions are stored as evidence rather than written straight through as edges. Only a repeated pattern crosses the threshold into a proposed edge.

### 5.2 Fields the edge needs

| Field | Why |
| --- | --- |
| origin | declared / observed / imported — so a typed-in spouse never outranks a guessed colleague |
| strength | 0 to 1, decaying with recency of interaction |
| confidence | How sure the extractor is that the mention resolved to this party |
| evidence_count | How many distinct interactions support the edge |
| last_interaction_at | Powers going-cold detection and strength decay |


### 5.3 The enum is kinship, not commerce

The 16 existing types cover guardian, dependent, spouse, next_of_kin and emergency_contact well — and commercial influence barely at all. referred_by, employer and represents are the only ones a seller would reach for.

Add: colleague · reports_to · introduced_by · co_attendee · champion · detractor · economic_buyer · knows

An observed edge is derived personal data. It has to appear in PrivacyRequest and be removable through privacy/forget/preview — both of which already exist here. That is precisely why this is safer to build on AgentSwitch than on the alternatives.

### 5.4 Why this matters at the deal, not just the contact

This is the part that turns a data-model improvement into a commercial differentiator. A deal is not a transaction with a company; it is a negotiation among people. The schema does not currently believe that.

Deal.create accepts products as an array and contact_id as a scalar. The author of that schema knew how to model a collection and chose not to for humans — so an enterprise deal with a six-person buying committee is recorded as a deal with one contact. Lead.create is thinner still, carrying only party_id. And Deal.source can be set to referral, partner or existing_customer while nothing anywhere records who referred, even though PartyRelationship already has referred_by and referred edge types sitting unused.

Once DealContact and the observed graph exist, each of these becomes answerable in one call:

| Deal question | How the graph answers it | Competitor equivalent |
| --- | --- | --- |
| Are we single-threaded? | Count confirmed DealContact rows with recent last_interaction_at. One contact on a large deal is the single best predictor of slippage | Gong and Clari sell this as deal risk |
| Who is the warm path in? | Relationship.path(our_party, economic_buyer) over colleague, introduced_by and referred_by edges | LinkedIn Sales Navigator TeamLink, outside the CRM |
| Has the champion gone quiet? | strength decay plus last_interaction_at on the champion edge | Attio follow-up assistant, at contact level only |
| Did our champion just leave? | employer edge closes with an until date; every open deal touching them flags | Nobody |
| Which relationships actually generate revenue? | Walk referred_by from every closed_won deal to find the people worth investing in | Nobody |
| Who else should be in the room? | Relationship.neighbors on the account, minus everyone already a DealContact | Salesforce relationship maps, manually maintained |


The last three are the interesting ones, because they are not features anybody is currently selling. They fall out almost for free once the edges exist and can be walked.

The commercial framing: every competitor sells pipeline intelligence — stage, value, probability, close date, all attributes of the deal record. AgentSwitch would sell relationship intelligence: who is involved, how strongly, through whom, and how that is changing. Deal risk stops being a probability field somebody typed in and becomes something derived from observed human behaviour, with the job ledger able to prove where every inference came from.

### 5.5 Why this one is not catch-up work

Attio models declared record relationships. Clay enriches individuals rather than networks. Day.ai builds customer memory from email and calls but not a traversable graph. Salesforce shelved Einstein Relationship Insights. Warm-path routing mostly lives in LinkedIn Sales Navigator — outside the CRM entirely.

Paired with the job ledger, AgentSwitch could answer "why does the system believe these two people are connected" with provenance no competitor can produce.


## 6. Defensible advantages

### 6.1 Shipped today, and unmarketed

The gap register is long, but almost all of it is catch-up on well-understood ground. These six are the opposite: already built, hard to copy, and currently not part of the pitch.

| # | Advantage | What it is | How to position it |
| --- | --- | --- | --- |
| 1 | Tamper-evident job ledger | AgentLedgerSeal with job_ledger.verify, .replay and .forensics — any agent action can be re-derived and proven after the fact | The answer to "prove what your agent did." Agentforce offers telemetry; none of the set offers cryptographic replay |
| 2 | Privacy in the runtime | PrivacyRequest, AgentPrivacyEvent, PrivacyAuditEvent, AgentRetentionRule, LedgerRetentionPolicy, and a forget/preview showing blast radius before deletion | DPDP and GDPR readiness as a platform property, not a policy document. Direct wedge into BFSI, healthcare and education |
| 3 | Governance before execution | AgentToolPolicy, AgentFloorConfig, AgentSeat and AgentPersona.daily_limits cap spend and reach per persona, with a hard block when exhausted | The counter to the unpredictable-bill complaint following Agentforce consumption pricing and Copilot Studio credits |
| 4 | Caller-scoped tool discovery | _meta.agentswitch.discovery: "caller" — tools/list returns only what the caller's roles, entitlements and row scope permit | Server-side enforcement versus Attio's client-side write confirmation. The boundary holds against a hostile client |
| 5 | Mission control for an agent workforce | staffing_forecast, incidents, incident_timeline, runbooks — agents as a fleet to be operated, not features to be toggled | Nobody else frames agents as a staffed floor; pairs naturally with per-seat and per-persona budget reporting |
| 6 | Storefront and CRM on one ledger | storefront.checkout, payment.verify, order.claim and collective booking write into the same permissioned record set as the CRM | Removes the commerce-to-CRM connector Zoho and HubSpot buyers pay an integrator to maintain |


### 6.2 The one to build: relationship intelligence on deals

The six above are defensive — they keep us in rooms we would otherwise be thrown out of. This one is offensive, and it is the only item in this report that is both not yet built and not held by anyone else.

| What it is | An observed, confirmable graph of who knows whom, walked at the deal level: buying committee, warm-intro path, champion decay, referral attribution (section 5) |
| --- | --- |
| Why it is ours to take | Attio models declared record relationships. Clay enriches individuals, not networks. Day.ai builds memory but not a traversable graph. Salesforce shelved Einstein Relationship Insights. Warm-path routing lives in LinkedIn, outside the CRM |
| Why it is safe here | Observed edges are derived personal data. PrivacyRequest and privacy/forget/preview already exist, so the graph is DSAR-clean from day one — the thing that stops most vendors shipping this |
| Why it is provable | The job ledger can show exactly which interactions produced a given edge, so "why do you think these two people know each other" has a cryptographic answer |
| Cost to get there | 12 engineer-days, one engineer, inside the three-week sprint. P0-8 unblocks it and P1-18 adds the deal committee in week 1 · P1-14 v0 captures and rolls up evidence in week 2 · P1-15 v0 makes it walkable in week 3 (7.3) |
| The pitch | Competitors sell pipeline intelligence — attributes of a record. We sell relationship intelligence — attributes of the people who decide |


Positioning note. This is the differentiator to lead with in deal conversations, but sequence it honestly. The schema lands in week 1, evidence capture in week 2, and traversal only in week 3 — so the demo exists at the end of the sprint, not during it. Two things not to overclaim: do not sell it before P1-15 ships, and do not describe the v0 rollup as a learned model. It is a threshold on repeated co-occurrence with a human confirm step — which is the more defensible claim anyway, because every edge traces back to the interactions that produced it.


## 7. Three-week delivery plan

Three weeks with two to three engineers is 45 engineer-days at full strength, and the register above totals well over a year of work. Breadth is still achievable, but not by working faster — only by exploiting one property of this catalog: the most valuable P0 gaps are horizontal. Aggregation, bulk writes, structured errors and progressive tool disclosure are each a single implementation at the shared CRUD and MCP layers, and each lands on all 51 entities and all 247 tools at once. Five gaps, five mechanisms, zero per-entity work.

That is the whole reason a three-week window closes 13 of 30 gaps instead of three or four.

### 7.1 Capacity and strategy

| Window | 15 working days |
| --- | --- |
| Team | 3 engineers, one per track (degrades to 2 — see 7.5) |
| Capacity | 45 engineer-days |
| Planned | 35 engineer-days across 13 gaps |
| Buffer | 10 engineer-days, 22%, unevenly distributed on purpose |


The buffer is loaded into week 3: weeks 1 and 2 run at 13 and 14 engineer-days, week 3 at 8. Traversal and tool discovery are the only week-3 builds, leaving the rest of that week for integration, seeding demo data and the review that three-week plans usually skip.

Three rules keep the breadth honest rather than aspirational:

Build mechanisms, not features. Anything that has to be written once per entity is out of scope by definition. Anything that can be written once at a shared layer is preferred even when its individual value is lower, because it multiplies across 51 entities.

Ship v0 scopes, and label them. Five of the thirteen land as deliberate first cuts rather than the full surface the register describes. They are marked v0 in section 4, and what each one leaves behind is named in 7.3. A v0 that is documented is a decision; a v0 that is not is a defect.

Touch nothing with an external dependency. GST e-invoicing needs IRP certification, outbound email needs deliverability warm-up, OAuth needs a consent surface. None of those compress no matter how the sprint is staffed, so none of them are in it.

### 7.2 Three tracks

The tracks are deliberately independent — they share no migrations and no code paths — so a slip on one does not stall the others.

| Track | Theme | Gaps | Days |
| --- | --- | --- | --- |
| A | Query ergonomics — one mechanism, every entity | P0-4, P0-1, P0-5, P0-2 | 11 |
| B | Relationship slice — the differentiator, end to end | P0-8, P1-18, P1-14, P1-15 | 12 |
| C | Discovery and breadth sweep — cheap wins on existing data | P1-13, P2-4, P1-8, P1-17, P0-3 | 12 |
|  | Total | 13 gaps | 35 |


Track B is sized to produce a working demo of relationship intelligence, not a production graph. That is the right trade for three weeks: the claim in 6.2 becomes demonstrable, and the schema it rests on is the part that is expensive to change later.

### 7.3 Week by week

Week 1 is schema and mechanism, week 2 is the work that depends on them, week 3 is traversal and integration. Day estimates are per item.

Week 1 — 13 engineer-days. Everything that other work depends on.

| Track | Work |
| --- | --- |
| A | P0-4 structured errors carrying missing[] and a readable message, at the validation boundary so all 247 tools inherit it (1d) · P0-1 generic {Entity}.aggregate with group_by and count, sum, avg, min, max (3d) |
| B | P0-8 the ActivityParticipant join table, migration and write path (3d) · P1-18 DealContact with roles and influence, plus referred_by_party_id on Deal (2d) |
| C | P1-13 v0 Party.find_duplicates and Party.merge on deterministic email, phone and normalised-name matching (3d) · P2-4 Cost.timeseries from nightly persona snapshots (1d) |


Week 2 — 14 engineer-days. The heaviest week; the relationship pipeline is the critical path.

| Track | Work |
| --- | --- |
| A | P0-5 bulk_create and bulk_update with a batch envelope and partial-failure reporting (2d) · P0-2 search.global spanning Lead, Deal, Party, Note and Activity (3d) |
| B | P1-14 v0 PartyMention evidence capture on every note, message and activity write, plus a nightly rollup proposing edges once co-occurrence crosses a threshold (4d) |
| C | P1-8 v0 AgentMemory.search as cosine top-k over embedded memory rows (3d) · P1-17 v0 outbound Webhook.subscribe on entity change, with retry (2d) |


Week 3 — 8 engineer-days. Deliberately light. This is where the demo gets built and the sprint absorbs its slippage.

| Track | Work |
| --- | --- |
| A | Load-test the generic aggregate path against the largest entities, document the new surfaces (2d) |
| B | P1-15 v0 Relationship.neighbors and Relationship.path by recursive CTE, with proposed edges routed to the existing AgentEscalation queue for human confirmation (3d) |
| C | P0-3 tools/catalog, tools/describe and search_tools per SEP-2636 (3d) |


#### What each v0 leaves behind

Naming this explicitly is the difference between a scoping decision and a hidden defect.

| Gap | Ships in the sprint | Left for later |
| --- | --- | --- |
| P1-13 | Deterministic duplicate detection and merge, with the merge itself written to the job ledger | Enrichment.resolve against any external provider |
| P1-8 | Cosine top-k over embedded memory rows | Reranking, hybrid keyword blending, recency weighting |
| P1-17 | Outbound webhooks on entity change, with retry and a dead-letter path | MCP resource subscriptions and listChanged notifications |
| P1-14 | Mention capture, threshold rollup, human confirm through AgentEscalation | A learned extractor, tuned strength decay, back-fill of origin across historical interactions |
| P1-15 | neighbors and path | subgraph and influence(deal_id) |


#### The demo this produces

By the close of week 3 an agent can answer two questions that are impossible today. "What did we sell this quarter, by owner?" becomes one SalesOrder.aggregate call instead of sixteen paginated round trips. "Who is the warm path into this deal?" becomes one Relationship.path call over edges that were never typed in by anyone — and because each edge carries its evidence, job_ledger.replay can show exactly which meetings and mentions produced it.

That second one is the whole pitch in 6.2, demonstrable in three weeks.

### 7.4 What is deferred, and why

Seventeen gaps are out of scope. They are not a backlog of things nobody got to — each is excluded for a specific reason, and the reason determines when it comes back.

| Reason | Gaps | Why three weeks cannot absorb it |
| --- | --- | --- |
| Gated on a third party | P1-1, P1-3, P1-16 | GST e-invoicing needs IRP certification, outbound email needs domain warm-up and deliverability reputation, OAuth 2.1 needs a consent surface and a security review. Calendar time, not engineering time — no staffing level compresses them |
| A product, not a tool | P0-6, P0-7, P1-2, P1-7, P1-9, P2-3 | The quote-to-cash chain, agent authoring with versioning and publish, ticketing with SLA clocks, call intelligence with transcripts, RAG with citation spans, and a policy DSL with simulate and diff. Each is a quarter-scale surface whose v0 would be misleading rather than useful |
| Unlocked by this sprint, not in it | P1-10, P1-11, P1-12 | Eval gating needs the authoring surface from P0-7 to gate. Forecast rollup gets much cheaper once P0-1 exists but still needs a quota and territory model. Scoring needs the graph plus interaction history to train on |
| Next up, weeks 4 to 8 | P1-4, P1-5, P1-6, P2-1, P2-2 | Campaign send, channel send on the five channels AgentSession already declares, meeting booking, async job handles, signed audit-pack export. All viable, all genuinely valuable, none load-bearing for the demo this sprint has to produce |


What not to promise during these three weeks. The two gaps that most often come up in buyer conversations — invoicing with GST filing, and agent authoring — are both deferred, and both are P0. That is the honest cost of choosing breadth-through-mechanisms plus one differentiator. If a live deal depends on either, this plan is the wrong plan and the sprint should be spent on P0-6 alone.

### 7.5 If the team is two, not three

Cut Track C whole. Do not thin all three tracks by a third.

|  | 3 engineers | 2 engineers |
| --- | --- | --- |
| Capacity | 45 days | 30 days |
| Planned | 35 days | 23 days |
| Gaps closed | 13 | 8 |
| P0 closed | 6 of 8 | 5 of 8 |
| Tracks | A, B, C | A, B |


Track A is the breadth engine and Track B is the differentiator; both lose their point if delivered partially. Track C is a collection of independent small wins, which makes it the only genuinely severable track.

The real loss is P0-3. Returning all 247 schemas on every initialize is a per-call cost every agent pays forever, and it is three days of work. If a third engineer becomes available for even one week, spend it there rather than on anything else in Track C.

### 7.6 Exit criteria

The sprint succeeded if each of these is true on the last day. Each is a behaviour, not a merged pull request.

One call returns revenue grouped by owner for a quarter, with no client-side summing. P0-1

Re-owning 300 leads is one call that reports per-record failures, not 300 calls. P0-5

Every -32602 response names the field that was missing. P0-4

One query returns matching Leads, Deals, Parties, Notes and Activities together. P0-2

A four-attendee meeting is stored as one Activity with four participants and their roles. P0-8

A deal carries a buying committee with roles and influence, and records who referred it. P1-18

At least one PartyRelationship row exists with origin = observed that no human typed, having passed through the AgentEscalation confirm queue. P1-14

Relationship.path returns a warm-intro route on seeded data, and job_ledger.replay shows the interactions behind every edge on that path. P1-15

An agent can work without receiving all 247 schemas upfront. P0-3

Criteria 7 and 8 are the demo. The rest are the foundation that makes the demo credible rather than a prototype.

### 7.7 The one real risk

Track B is the critical path and it is serial: P0-8 in week 1 blocks the rollup in week 2, which blocks traversal in week 3. A slip on the ActivityParticipant migration costs the differentiator demo outright. It is therefore the first thing started and should be the first thing reviewed, on day 3 at the latest.

The genuine unknown is the rollup threshold in P1-14. On sparse data, co-occurrence scoring tends to propose either nothing or everything, and there is no tuning set to calibrate against. Three things contain that:

The human confirm step means a bad threshold produces a noisy queue, not a polluted graph. Precision failures stay visible and reversible.

Seed realistic interaction data in week 1, not week 3, so the threshold has something to tune against before traversal is built on top of it.

The fallback is to set the threshold at a single co-attendance and let the confirm queue do the filtering. Less impressive as automation, equally demonstrable as a graph, and still more than anyone in the comparison set ships.


## 8. Appendix

### 8.1 Method

The catalog inventory was computed directly from the tools/list response: tools were bucketed by name shape ({Entity}.{op}, endpoint.*, and permission: submit for transitions), then grouped by entity to derive per-entity operation coverage. Behavioural claims — pagination limits, error shapes, response envelopes — come from live tools/call probes rather than schema reading.

Competitor ratings come from vendor documentation and 2026 comparison coverage, not hands-on testing of each product. Treat borderline "partial" ratings as directional.

Sprint sizing in section 7 converts the register's S/M/L bands into engineer-days at the low end of each band, on one explicit assumption: that a mechanism implemented once at the shared CRUD or MCP layer needs no per-entity follow-up work. If that assumption is wrong — if aggregation or bulk writes turn out to need per-entity handling — Track A expands past its 11 days and the plan loses Track C before it loses anything else. These are planning figures derived from schema inspection, not commitments from the engineers who would build it. The 22% buffer in 7.1 exists because five of the thirteen items are v0 scopes whose boundaries will move on contact with the code.

### 8.2 Sources

Catalog and probes

initialize, tools/list, SalesOrder.list, SalesOrder.get against https://agentswitch.theschoolofai.in, 2026-09-17.

MCP protocol

MCP specification 2026-07-28 — server tools

MCP client best practices

SEP-2636: Progressive Tool Disclosure

AWS Prescriptive Guidance: MCP strategies

Observability tools agents want — on aggregate query design

Enterprise agentic CRM

Copilot Studio vs Agentforce 2026

Breeze vs Agentforce 2026

Best AI CRM agents 2026

AI-native MCP CRM

Attio MCP overview · Attio MCP operator review 2026

Day.ai product

AI-native CRM platforms compared 2026 · Attio vs folk vs Clay 2026

India SMB CRM and ERP

Zoho CRM pricing

ERPNext vs Odoo for Indian small business

Best AI CRM software for Indian businesses 2026
