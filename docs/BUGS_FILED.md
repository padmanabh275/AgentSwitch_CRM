# Team 6 — bug reports filed against AgentSwitch

Seat `team06@theschoolofai.in` · Pipeline/crm · Suryodaya
`https://agentswitch.theschoolofai.in` · company `5cbe5a55-af74-4363-a436-f5350593114c`

**32 reports on file, all status `new`** (checked live 2026-10-15). 11 filed 2026-09-22 in the
gap-analysis session; 9 from the 2026-09-28 bug hunt (see *Filed from the 2026-09-28 bug hunt* below).
`/api/bug-report/mine` and `BugReport.list` return the same 31.
Live source of truth: `GET /api/bug-report/mine` (saved to `out/suryodaya/bugreport_mine_final.json`).
Report drafts in `drafts/`. `BugReport` has no `update`, so nothing here can be edited after filing.

---

## Why this matters: what the sales agent can and cannot do today

The agent's job is to carry a goal across the funnel — qualify a lead, work a deal, build and send
a quotation, land a confirmed order — and to answer questions about the book while doing it.

**The mechanics work.** The 12-step chain `Lead → Deal → Quotation → SalesOrder → confirmed` runs
end to end (verified, `SALES_AGENT_PATH.md`). Transition guards are enforced and errors are
self-correcting (`UNHAPPY_PATHS.md`). Nothing below blocks us from starting to build.

**What the reports below fix falls into two kinds:**

1. **Defects that make the agent confidently wrong.** Four of the five fail *silently and plausibly*
   — the write succeeds, the value persists, it reads back intact, and nothing downstream reveals
   the record is wrong. A missing tool stops the agent; a silent wrong value gets propagated. These
   are the expensive ones.
2. **Gaps that leave the agent unable to act at all.** No aggregate, so no sales question about
   totals is answerable. No send, so nothing the agent decides ever reaches a customer.

### Ranked by impact on the agent

| Rank | id | Agent cannot… | Workaround |
|---|---|---|---|
| 1 | `2e4d571b` | contact anybody, on any channel | none |
| 2 | `a81bd641` | answer "what's the pipeline worth" | 7+ calls, unverifiable arithmetic |
| 3 | `5f44fa94` | trust any lead search it runs | none — failure is invisible *(no longer reproduces, 09-28)* |
| 4 | `a6899ff7` / `791d32e3` | tax a quote or order correctly | use record-level `taxes[]` |
| 5 | `df88720f` | be trusted with billing state | never write the field |
| 6 | `ffffe122` | avoid forking the customer list | search-before-create, best effort |
| 7 | `5e81d46f` | record who was in a meeting | none — schema is closed |
| 8 | `a602d95a` | say who decides a deal, or who referred it | none — schema is closed |
| 9 | `4154f39f` | resolve a name cheaply | 7 calls per lookup |
| 10 | `a55aac01` | read an order's provenance | use `quotation_id`, not the display |

---

## Session of 2026-09-22 — 11 reports

### Defects — behaviour contradicts the platform's own contract

#### `a6899ff7` · Per-line GST on Quotation never reaches the totals
**Evidence.** Controlled A/B, same Rs 1,000 + Rs 180 tax. Per-line (QTN-2026-00058) →
`total_tax 0.0`, `grand_total 1000.0`. Record-level `taxes[]` (QTN-2026-00059) → `180.0` / `1180.0`.

**Why the agent needs this fixed.** Building the quotation *is* the agent's core job — step 8 of the
12-step chain. `Quotation.create.items` advertises 25 per-line tax fields, and per-line GST is the
semantically correct way to express Indian tax, so a schema-driven agent will reach for it. It then
re-reads the record to verify its own work — which we require it to do — sees its tax sitting on the
line items, and concludes the quote is right. It is short by the entire GST amount. There is no
error to catch and no discrepancy visible without independently re-adding the lines. **The agent
cannot self-check its way out of this one.**

#### `791d32e3` · Same defect on SalesOrder, the billing document
**Evidence.** SO-2026-00237: per-line cgst 90 + sgst 90 → `total_tax 0.0`, `grand_total 1000.0`.
Chain-built orders are unaffected.

**Why the agent needs this fixed.** A quotation is an offer and can be corrected before acceptance.
A SalesOrder is what gets delivered and billed — it carries `invoiced_status` and `billed_qty`, and
every downstream financial number derives from its `grand_total`. This is the agent's terminal step,
so an error here is the one nobody catches before money moves. A fix applied only to Quotation would
leave this exposed.

#### `5f44fa94` · `Lead.list` silently ignores its `search` filter
**Evidence.** Every query returns all 143 rows, including `"zzzzz-no-such-lead-qqq"`. The same
nonsense query returns 0 on Party, Deal and Activity. Lead's other filters work (`status=qualified` → 7).

**Why the agent needs this fixed.** Lead is the entry point of the funnel, so this is the search the
agent runs most often. It asks for leads matching a company name and receives 143 unrelated ones,
with `total: 143` and no signal the filter was dropped. **The agent cannot distinguish a broken
filter from a genuinely broad match**, so it takes the first row and proceeds — and every Deal, Note
and Activity it then creates attaches to the wrong lead. A human notices instantly; an agent does not.

#### `df88720f` · `invoiced_status` is writable on a draft order, then locks
**Evidence.** SO-2026-00237 flipped `unbilled` → `invoiced` with `billed_qty 0` and no Invoice
entity in existence. Confirmed orders refuse such writes (verified on SO-2026-00236), so the wrong
value is permanent.

**Why the agent needs this fixed.** Because there is no Invoice tool, `invoiced_status` is the only
billing-shaped field the agent can reach. An agent told to "mark this order billed" has exactly one
field that looks right and nothing telling it otherwise. The mutable window — draft, before confirm
— is precisely the stretch the agent passes through on every deal, and the value locks on the way out.

#### `a55aac01` · `_quotation_id_display` renders the customer, not the quotation
**Evidence.** 5/5 orders wrong. SO-2026-00060 shows `Vardhman Aerospace SEZ Unit`; correct is
`QTN-2026-00017`. Audited 470 display fields across 16 entity/field combinations — 465 correct.

**Why the agent needs this fixed.** The agent re-reads records as state changes underneath it. If it
uses this field to answer "which quote did this order come from?" it gets a company name — and
potentially attributes the order to a company unrelated to it, since the order already displays its
real customer in an adjacent field. Two plausible company names, neither marked wrong. *Lowest
severity of the five: the agent can read `quotation_id` and resolve it itself.*

### Capability gaps — the agent cannot act without these

#### `2e4d571b` · Seven declared channels, zero send tools
`AgentSession.create.channel` ∈ `{web, whatsapp, telegram, slack, discord, email, api}`; nothing in
238 tools sends on any of them. `Quotation.send` is a state transition — `{id}` only, no recipient,
delivers nothing.

**Why the agent needs this.** A sales agent's entire loop is follow-up: send the quote, chase the
proposal, confirm the meeting. Today the agent can decide to do all of it and transmit none of it.
There is no primitive to orchestrate around — this is not a missing convenience. Worse, moving a
quotation to `sent` while nothing is sent means **the record asserts a customer communication that
never happened**, which is more damaging than failing outright.

#### `a81bd641` · No aggregate or group-by anywhere
310 SalesOrders, 143 Leads, 135 Deals — all paged at 20 (max 50).

**Why the agent needs this.** "What is the pipeline worth", "revenue this quarter by owner", "what
closes this month" are the questions a sales seat exists to answer, and every one is an aggregate.
Today each means paginating hundreds of ~50-field rows through the model and re-adding them there —
slow, unverifiable, and not numerically trustworthy. The spend cap makes it worse: a large aggregate
can be cut off mid-run, and **a partial sum is indistinguishable from a complete one** in the
agent's own output.

#### `ffffe122` · No duplicate guard on `Party.create`, no merge
All 194 parties are currently clean — filed as preventive, not as existing damage.

**Why the agent needs this.** The agent creates parties from inbound leads, resolving names
heuristically. "Bharat EV Motors Ltd", "Bharat EV Motors" and a typo are one customer to a human and
three `Party.create` calls to an agent that searched, got an ambiguous answer and decided to create.
Nothing stops it, detects it afterwards, or repairs it. The damage compounds silently: once forked,
subsequent deals and activities attach to whichever copy it found, so that customer's pipeline value
splits across records and every aggregate about them is wrong. **Prompting cannot make this safe —
it is a judgement call under ambiguity, which is exactly where a server-side guard belongs.**

#### `5e81d46f` · Interactions can name only one person
`Note.create` / `Activity.create` take a scalar `party_id`; schemas are closed.

**Why the agent needs this.** The agent files notes and activities after every interaction, and in
B2B sales most involve several people. Both available encodings are lossy: one row that silently
drops attendees, or N rows that nothing links — inflating every meeting count by N. The agent cannot
answer "who was in that meeting" because it was never representable. It also starves the
relationship graph: `PartyRelationship` ships full CRUD over 16 edge types and sits at `total=0`,
because co-attendance — its richest input — cannot be expressed.

#### `a602d95a` · One contact per deal, and no referrer
`Deal.create` models `products` as an array but `contact_id` as a scalar. `source` accepts
`"referral"` with nowhere to record who referred — 6 such deals on one page.

**Why the agent needs this.** "Are we single-threaded on this deal?" is the strongest slippage
signal a sales agent could offer, and it is unanswerable when a six-person buying committee is
stored as one contact. The referral case is sharper: the agent can *set* `source="referral"` — the
enum invites it — and the value is unactionable the moment it is written. `PartyRelationship`
already defines `referred_by`; Deal simply cannot reach it.

#### `4154f39f` · No cross-entity search
Per-entity `search` works — we are only asking for the fan-out.

**Why the agent needs this.** `party_id` is a required argument of almost every write tool, so
resolving a name to an id is the agent's first move in nearly every task. Today that is 7 separate
paginated calls plus client-side merge, repeated constantly, against a token budget. It also behaves
inconsistently — the same query hits on some entities and returns 0 on others — and the agent cannot
tell "no match" from "this entity's search doesn't cover that field".

---

## Earlier and later reports — 12 (5 summarised without detail, see note below)

| Date | id | Finding |
|------|----|---------|
| 09-16 – 09-17 | *(5 reports)* | **Seat-isolation and cross-app access findings.** Filed and unresolved; details held in the internal register rather than published here, since the instance is shared with other teams. |
| 09-17 | `8ae6f7c8` | Deal transitions ignore their source-stage guard ⚠️ **see below** |
| 09-17 | `17e054a8` | Lead transitions ignore their source-status guard ⚠️ **see below** |
| 09-18 | `cc1823e3` | Clicking on domain does nothing (UI) |
| 09-22 | `62fa027c` | Live `tools/list` is 238, not the saved 247 (now 242 as of 09-28) |
| — | `4e90fc79` | `reason_code` has no value that means anything across apps |
| 09-28 | `87d456e5` | `Deal.update` advertises `stage` as writable, then rejects every value, including legal ones |
| 10-15 | `b1a06b93` | `tools/list` advertises four `endpoint.manufacturing.*` cost tools (`standard_cost_rolls.frozen`/`.drafts`, `purchase_price_variance`, `job_margin`) that 403 on every call. Draft: `../drafts/BUG6_cost_tools_listed_but_403.txt`. ✅ **Fixed in Release 13 (N481)**, see below |


> **Note on scope.** Five reports covering seat isolation and cross-app access are summarised
> without detail above. They concern a live instance shared with other teams and remain unresolved,
> so the specifics stay in Team 6's internal register. Everything else here is a functional defect
> or capability gap in the sales/CRM surface and is reproduced in full.

---

## ✅ Fixed in Release 13 — 8 (21 Oct 2026, 14:05 IST, Suryodaya and Keystone)

The instructor's release notes list these as fixed for team06. N-numbers are the instructor's
ticket numbers. "Live check" is our own re-check on 2026-10-22, reads and refused calls only.
N480, N482 and N483 only show after a write. They are covered by harness write tasks, which run
on the hosted copy or locally with `--write`, and have not yet been run live.

| N | Our report | Fix | Live check 10-22 | What changed on our side |
|---|---|---|---|---|
| N479 | seat isolation (summarised above) | Endpoint tools for apps the seat doesn't hold (storefront, email lists, collective, people directory) are no longer listed or callable | ✅ none listed; `endpoint.storefront.products` → `tool_not_available` | `composite_bom_hidden` checks with `tools_absent`, on both instances |
| N480 | `drafts/BUG7` | A quote line may have no rate; submit for margin approval, the approver prices it. An unpriced quote can't be sent, accepted or converted | ✅ `Quotation.create` line requires only `qty` | `write_quote_for_approval` replaces `refuse_unpriced_quote_rate_required`; TO_REVISIT #11 resolved |
| N481 | `b1a06b93` (`drafts/BUG6`) | Manufacturing cost tools that always 403'd are no longer listed | ✅ no `manufacturing` tool in 234 | same `tools_absent` check |
| N482 | `drafts/BUG8` | `Lead.convert` on a lead that already has a deal links that deal instead of making a second | write needed | agent converts instead of refusing and checks the deal is the same one; `convert_lead_with_deal` replaces `refuse_convert_lead_with_deal` |
| N483 | `drafts/BUG10` | Quote → sales order links the order to its deal and marks the deal won | ⚠️ partly. Live 10-22 via `convert_to_order.accepted`: the order is always linked (`deal_id`), but the deal is won **only if its value is non-zero**. A value-0 deal stays at `negotiation` with no error (`Deal.mark_won` on it says "Set a value first"). With a value set, the platform won it (batch `20261022T171147`) | agent reads the deal back and reports its stage, and says why when a value-0 deal stays open; `quote_ordered`'s platform checks report `error`, not `fail`; quote fixtures now give their deal a value |
| N484 | `drafts/BUG9`, `drafts/BUG11` | Errors name only steps the seat can take; wrong-state errors carry `invalid_transition` | ✅ draft → accept/decline offers only "Submit for Margin Approval; Send" (no Cancel/Expire), code `invalid_transition` | none needed |
| N485 | — | `skill.export` with no selector → `invalid_arguments` naming the fields | ✅ `endpoint.agent.skill.export {}` → `invalid_arguments`, field `/`: "Provide one of: skill_id, persona_id, persona_name." | none (agent doesn't use it) |
| N487 | `01f3201b` (D6) | Provider filters accept the older provider names | ✅ enum now has `claude`/`anthropic`, `gemini`/`google`, … | none (agent doesn't use it) |

**Possible follow-up for the instructor (not filed):** N483's silent skip. Converting a quotation on
a deal with value 0 makes a linked order but leaves the deal open, with no error or warning on the
conversion. Our BUG10 probe deal was value 0, so as filed the report would still reproduce. Either the
conversion should set the deal's value from the order, or it should say why the deal wasn't won. Evidence:
SO-2026-00242 on deal `ff2256d0` (value 0, stayed `negotiation`, batch `20261022T170441`) vs SO-2026-00244
(value set, `closed_won`, batch `20261022T171147`). The task `order_on_value_zero_deal` tracks the gap. While the gap
stands, it passes when the deal stays open and the agent says the value is 0. If the platform starts winning a value-0
deal, its `value_zero_deal_left_open` check reports `error`, as a reminder to close this note.

**Another possible follow-up (not filed):** a quote taken back out of margin approval can't be sent. Once a quote
is submitted, `Quotation.revise_quote` puts it back to `draft` but leaves `approval_status` at `pending_approval`
with no approval request (`approval_request_id` null). `Quotation.send` is then refused: "Transition 'Send' is guarded
by `approval_status != 'pending_approval' and approval_status != 'rejected'`". It may be intended (the margin
check can't be skipped by revising), but the quote waits for an approval nobody was asked for, and only
resubmitting gets it out. Evidence: QTN-2026-00083, batch `20261022T212902` (the funnel's first hand-off, which
tried to price and send the agent's quote). The funnel's hand-off now makes a separate quote instead.

Seen while checking: `Quotation.make.SalesOrder` on an already-converted quotation returns the
existing order (our test order SO-2026-00241) instead of refusing, so a repeat call is idempotent
and makes no second order. We did not file this.

## ⚠️ Two filed reports may no longer reproduce

`8ae6f7c8` and `17e054a8` claim Deal/Lead transitions ignore their encoded source-state guard.
**Six illegal-transition probes on 2026-09-22 were all correctly rejected** — including
`Deal.mark_won.negotiation.closed_won` on a `new` deal, a repeated `Deal.qualify`, and every
transition against a `closed_lost` deal. Each returned `invalid_transition` with a precise message.

Either these were fixed after 17 Sep or the original repro differed. **Re-check before citing them
to the app owner.** Evidence: `out/suryodaya/unhappy/` and `UNHAPPY_PATHS.md`.

## Filed from the 2026-09-28 bug hunt — 9

Full drafts, evidence and the ruled-out list are in `bug-hunt-2026-09-28.md`. Nine of the ten
drafts are filed; D10 was dropped. Dates are the platform's `created_at` (UTC).

| # | id | Filed | Finding | Severity |
|---|---|---|---|---|
| D1 | `d99a804e` | 09-27 | `Deal.create` accepts `stage: "closed_won"`, bypassing every transition guard | high |
| D2 | `8e3fb3cd` | 09-27 | `sort_order` on `Item`/`Pipeline`/`AddressBook.list`: every legal value is a 500 ⚠️ **see below** | high |
| D3 | `58ea5034` | 09-27 | List schemas advertise create-time defaults and placeholders on filters (`Deal.list` goes to 0 of 143) | high |
| D4 | `92f40dae` | 09-27 | `AgentSession.update` stores negative tool-call, token and cost counters | medium |
| D5 | `8a9b1cc1` | 09-27 | `Deal.create` accepts any currency string (`ZZZ`) | medium |
| D6 | `01f3201b` | 09-27 | `AgentProvider.list` `provider` enum allows 1 of the 7 providers that exist ✅ **fixed in Release 13 (N487)** | medium |
| D7 | `4a0f2d5d` | 09-27 | `ContactGroupMember.list` ignores `search` (same class as `5f44fa94`) | medium |
| D8 | `2ad62cbf` | 10-05 | An unknown `sort_by` on `Deal.list` / `Party.list` errors with no `data.code`, unlike every other argument error | medium |
| D9 | `c68f1d54` | 10-04 | An escalation created without `channel` is never raised, routed or given an SLA | medium |
| D10 | — | — | *Not filed.* `status` advertised and rejected on five more entities | dropped |

- **D8 was narrowed before filing.** Re-checked 2026-10-05 (reads only): the `sort_by` case still
  has no code, while bad `probability` returns `invalid_arguments` and a missing id `not_found`.
  The draft's REST-path claim (guard messages saying "Use POST /api/…/transition") was not
  re-checked, since triggering it needs a write, and is left out of the report.
- **D9 was filed with fresh numbers:** 70 escalation rows, 69 with a `channel`, not the draft's 26.
  An earlier attempt, `10b6134c` (2026-10-04), was filed without `company_id`/`reporter` and is
  invisible to this seat.
- **D10 dropped (2026-10-05):** `status` is no longer in the `AgentSession`, `AgentTask`, `Lead`,
  `Quotation` or `SalesOrder` `.update` schemas. Only `Quotation.create` still offers it
  (`default: "draft"`), too thin for a report of its own.

## Claims in our own register that no longer hold

**Re-checked 2026-10-05:**

- **D2 (`8e3fb3cd`) may be fixed.** `Item.list` with `sort_order: "asc"` now returns a coded
  `invalid_arguments` ("/sort_order must be number") instead of a 500. The schema is still `number`
  where 46 other lists take `asc`/`desc`, so the inconsistency stands; the 500 does not. Not
  re-checked on `Pipeline` or `AddressBook`.

**Re-checked 2026-09-28:**

- **`5f44fa94` (Lead search ignored) no longer reproduces.** Nonsense search returns `total: 0`.
- **`a81bd641`'s "paged at 20 (max 50)"** is out of date. `limit` now goes to 1000, and one call
  returns all 143 deals or all 312 orders. The aggregate gap itself stands.

Verified against the live 238-tool catalog on 2026-09-22 — strike these before the register goes out:

- **Gap report P0-3** (no tool catalog / describe / search) — `tools.search` and `tools.describe`
  have **shipped**.
- **Gap report P0-4** (errors return `path: "/"` and don't name the field) — **fixed.** Errors now
  return `field` plus an actionable message, and bad enums return the entire allowed set.
- **`pipeline-bugs.md` P1-1** (`products` vs `items` rename breaks copy-forward) — largely a
  non-issue: `Deal.make.Quotation` maps them automatically. The error only appears on hand-copy.
- **Four candidates died under verification** and were deliberately not filed: GST-integrity (tax
  *is* correctly classified as CGST+SGST / IGST on all 35 orders), delivered-but-unbillable (a
  capability gap the strategy docs already argue), `convert_to_order.sent` bypass (advertised in
  the tool name, same standard that dropped `mark_won.new`), and terminal-record mutability
  (confirmed orders reject everything).

## Test records left in the shared book

No delete tool on this seat, so these persist. Team 07 shares this book.

`QTN-2026-00058` · `QTN-2026-00059` · `QTN-2026-00060` · `SO-2026-00236` · `SO-2026-00237`
(deliberately left with a false `invoiced_status` so `df88720f` can be verified) · plus `T6-unhappy-*`
and `T6-chainwalk-*` leads and deals · plus the 2026-09-28 hunt's `T6-hunt-*` records: a party, a
`ZZZ`-currency deal, a `closed_won` INR 1 deal and a session with negative counters (ids in
`bug-hunt-2026-09-28.md`).
