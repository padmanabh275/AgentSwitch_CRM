# To revisit later

Open questions and decisions parked mid-conversation — not blocking, but not
settled either. Each entry: what's undecided, why it came up, and where the
relevant code/docs live.

---

## 1. Should open deals with no close date count as "at risk"?

**Raised:** 2026-09-26, first live run of the task graph.

**Context:** 69 of the 88 open deals have no `expected_close_date`. The at-risk
rule ("close date in the past") can't judge them, so it only covers 19 deals.
They're currently listed separately in the finding as
`open_without_close_date_ids` and mentioned in the answer, but not flagged.

**Question:** leave the rule as is; flag them as at risk ("no close date" is a
risk in itself); or add a second signal, such as staleness (`updated_at`) or
`probability`, planned for Phase 3.

**Where:** `agent/domain/deals.py` → `at_risk()`.

**Status:** unresolved. It changes the graded definition, so decide before the
tests are written.

---

## 2. Why are our escalations unassigned?

**Raised:** 2026-09-26.

**Context:** `ESC-2026-00026` (filed by this agent) has `assignee_user_id`,
`assignee_display` and `raised_at` all null. Team 04's `ESC-2026-00025` is
assigned to Meera Kulkarni with `channel: "api"` and a 240-minute SLA. The old
README claimed our escalations reach Meera; the record says otherwise.

**Question:** what Team 04 does differently: passing `channel`, filing through
the REST governance endpoint, or a separate assign/raise step. Until that's
known, "escalated" may mean "recorded, but nobody is notified".

**Where:** `agent/domain/escalation.py`; Team 04's filing code in
`../AgentSwitch_team04` (capstone working folder).

**Status:** unresolved. Investigate before relying on the escalation path in a demo.

---

## 3. Is `Item.standard_rate` the "real BOM price"?

**Raised:** during design as "escalate the BOM quote, or solve it?".
**Largely resolved 2026-09-26: solve it.**

**Context:** the brief assumed the BOM price was unreachable, so every design
pass escalated the quote. A full `Item.list` read showed `standard_rate` is a
read-only field set only on items with a BOM (27 of them). The agent now
quotes `standard_rate` × qty and escalates only when it's unset. List prices
are never used in its place.

**Still to confirm with the instructor:**
- that `standard_rate` is the intended field. The synthetic values are odd:
  it's often far above `selling_price`.
- that reading it is fine under the brief's "don't sneak cross-app data
  through embedded fields" rule. It's a documented read-only field on
  `Item`, which this seat owns.

**Where:** `agent/domain/items.py` → `BOM_PRICE_FIELD`, `price_lookup()`.

**Status:** implemented; waiting on instructor confirmation.

**Update 2026-10-05: the platform has answered part of this.** `standard_rate`
(and `standard_rate_updated_at`) are now in `Item._redacted_fields` for this seat
and read as null on every item, so the sales seat is not meant to see BOM costing.
Every quote now escalates, with the reason saying the price is hidden from this seat
rather than unset (harness task `composite_bom_hidden`). Still worth asking the
instructor whether a sales quote "off the real BOM price" is meant to be an
escalation by design, or whether another route exists.

---

## 4. Which item to use for the quote demo?

**Context:** now that the quote uses `standard_rate`, the demo wants one item
per outcome:
- **quoted**: a sellable item with a BOM price, e.g. Toolmaker's Vice 105mm
  (Mtr) (`SP2319/3567 #3567`, ₹6,02,246.98).
- **escalated, has a BOM but no BOM price**: SuryaTools Bench Vice 150mm
  (`ST-VICE-150`). It's also the product in the "Nashik ITI bulk order, 60
  vices" deal.
- **escalated, no BOM**: Torsion Spring SS304 (`ASP-H-011`, `dd873bd7-…`),
  the item used so far.
- **refused**: a nonexistent id, or an ambiguous name like "bench vice".

**Status:** candidates picked; confirm they suit the demo.

---

## 5. The first run that writes to the platform

**Context:** every Phase 1 run so far used `--dry-run`. The first run without
it will create a `T6-` session, an AgentMemory, and a **new** escalation. The
subject is now `T6-BOM quote for …`, so it won't match the older unprefixed
`ESC-2026-00026`, which is still `open`.

**Question:** run it supervised once (after #2), and decide whether to withdraw
the stale `ESC-2026-00026`. Withdrawing is a write to a record other people can see.

**Status:** unresolved. Needs explicit go-ahead.

---

## 6. Should writes be opt-in?

**Context:** writes currently happen unless `--dry-run` is passed. An
accidental plain run creates platform records and possibly an escalation. The
alternative is to default to dry run and require `--write`.

**Where:** `agent/run.py`.

**Status:** unresolved. The current default matches what the brief expects of
a real run.

---

## 7. Should our own `T6-` test deals be excluded from the lists?

**Context:** earlier probes left `T6-…` deals in the shared book (e.g.
`value: 1.0`). None were in either list on 2026-09-26, but a test deal with a
close date this month would be counted.

**Question:** exclude titles starting with `T6-`, flag them, or leave them in.

**Where:** `agent/domain/deals.py`.

**Status:** unresolved, low priority.

---

## 8. Is re-login on HTTP 401 the right trigger?

**Context:** `MCPClient.call` logs in again and retries once on HTTP 401. That
an expired token produces a 401 (rather than, say, a 200 with an error body)
is an assumption; it hasn't been observed.

**Where:** `agent/transport/mcp_client.py`.

**Status:** unverified. Check when a long-running session actually expires.

---

## 9. Should the quotation carry GST?

**Raised:** 2026-10-15, adding the quotation write (`--deal`).

**Context:** the agent drafts the Quotation at `standard_rate` × qty with no
`taxes[]`, matching the quote section ("before tax and discounts"). A real
quote to an Indian customer carries GST, which needs place of supply and
the customer's registration; the deal carries neither. If tax is added it
goes in record-level `taxes[]` (per-line tax fields don't reach the
totals, bug `a6899ff7`).

**Where:** `agent/domain/quotations.py` → `quotation_args()`.
`quotation_written` checks `grand_total = net + tax`, so it holds either way.

**Status:** unresolved.

---

## 10. First live run of the write tasks

**Context:** `write_quote_on_deal` and `write_quote_stale_deal` have passed
only an offline rehearsal against an in-memory platform. A live
run (`python -m harness.runner --write --tasks write_quote_on_deal,write_quote_stale_deal`)
creates, per task, a `T6-harness-…` lead and deal in the shared book, and
for the first task a sent quotation. The fixture deals are marked lost
afterwards. Not yet confirmed live: that `Quotation.create` accepts
`deal_id` + `items` as sent, and that `Quotation.send` is still the name
of the draft → sent transition.

**Status:** needs explicit go-ahead (see #5).

**Update 2026-10-15:** run live with go-ahead. The priced pair can't run:
`standard_rate` is redacted for this seat on Item.list/get (saved runs show it
visible on 2026-09-26 and redacted by 2026-10-05; it was only noticed today), so the
fixture finds no priced item and errors before writing anything.
`AgentEscalation.create` has also left the seat's catalogue
(`tool_not_available`); the escalate path now returns a refusal saying so
instead of erroring. Two tasks were added on the platform's new route
(`Quotation.submit_for_margin_approval` draft → pending_approval):
`write_quote_for_approval_stale_deal` **passed live, 22/22** (batch
20261015T224411); `write_quote_for_approval` fails at `Quotation.create`
with `invalid_arguments`, because a line item requires `rate` and this
route sends none (see #11). Also fixed live: `AgentSession.update` rejected
`total_tool_calls` (not in the schema), and `Lead.make.Deal` without
products returns a wrapper the old unwrap didn't recognise.

---

## 11. What rate goes on a quotation line the seat can't price?

**Raised:** 2026-10-15.

**Context:** Quotation.create requires `items[].rate`. The BOM price is
redacted from this seat, the cost basis and margin fields on Quotation are
too, and there's no escalation tool any more. Options: (a) rate 0 as an
explicit placeholder, submitted for margin approval — a ₹0 line on a
customer document, which the brief's "never invent a price" may rule out;
(b) a quotation with no lines and the request in `notes` (items isn't
required at the top level) — untested, and margin approval of an empty
quote may be meaningless; (c) ask the instructor what the intended route
for an unpriced item is now.

**Status:** open. Until decided, `write_quote_for_approval` fails honestly
and no quotation is written.

**Update 2026-10-16: refusal is the default.** `draft_for_approval` now reads
the live `Quotation.create` schema each run (`line_rate_required`). If
`items[].rate` is required, it refuses before any write, with the reason. If
the platform drops the requirement, it drafts with no rate as before. Nothing
is hardcoded to 0. `write_quote_for_approval` is replaced by
`refuse_unpriced_quote_rate_required` (verifier `unpriced_quote_refused`).
Its premise check errors if the rate stops being required.

Why not a ₹0 placeholder by default: a draft can go straight to `sent`
(`Quotation.send`), and a sent quote can become an order without acceptance
(`convert_to_order.sent`). So if anything skips margin approval, ₹0 reaches
a customer, or even a sales order. This seat has no `Quotation.delete` or
cancel, so a failed submit would strand a ₹0 draft. The margin fields are
redacted, so we can't confirm ₹0 even triggers approval.

**If (a) is chosen later, as an opt-in fallback, not a default:**
1. A setting `UNPRICED_PLACEHOLDER_RATE`. Unset means refuse (today's behaviour).
2. Use it only if the margin-approval transition is in tools/list (checked
   **before** the create) and the deal re-read shows it still open.
3. Create, then submit at once, then read back. Anything but
   `pending_approval` is an error that names the stranded quotation.
4. The line description says "placeholder, not a price; set at margin
   approval". The finding reports `rate_source: placeholder`, never as our
   price.
5. The verifier checks the stored rate equals the setting, the status is
   `pending_approval`, and no send was made.
6. Needs one live write to confirm ₹0 is accepted and routed to approval.

---

## Resolved

### Is `AgentMemory.create` worth keeping as the finding-storage step?

**Raised:** 2026-09-26. **Resolved:** 2026-09-26 — **kept.**

No purpose-built "submit your finding" tool exists, `/api/agent/evidence/*`
403s for this seat, and `AgentMessage` turned out to be read-only (no
`create`). So `AgentMemory.create` (`category: "context"`) remains the
platform copy of the finding. It's no longer the only record: every run also
saves `runs/<run_id>/taskrun.json` locally first, and a failed AgentMemory
write is only a warning.
