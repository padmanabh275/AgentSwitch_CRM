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

## 3. Should a list price answer a "real BOM price" question? (now urgent)

**Raised:** during design; **escalated in priority 2026-09-26.**

**Context:** the design assumed every Item price field was `0.0`, so the quote
would always escalate. A full read shows **94 of 103 items have a sell-side
list price**. The agent therefore *quotes* most items from the list price
(labelled "not a BOM-derived cost"), and escalates only the 9 unpriced ones.
For the natural demo item, SuryaTools Bench Vice 150mm, it answers 500 ×
₹5,799 = ₹28,99,500. The brief says the BOM lives in manufacturing, that this
seat can't reach it, and that the correct behaviour is escalate or refuse,
never invent. A list price isn't invented, but it isn't the BOM price asked
for either.

**Options:**
1. Keep as is: quote the list price, clearly labelled.
2. For a "BOM price" request, always escalate, and put the list price in the
   escalation as context.
3. Quote the list price *and* escalate for the BOM cost.

**Where:** `agent/domain/items.py` → `price_lookup()`; `graph/planner.py`
(the escalation rule).

**Status:** unresolved. Decide before writing the graded tests: it flips the
expected `quote.outcome` for most items. Ask the instructor if unsure.

---

## 4. Which item to use for the quote demo?

**Context:** the item used so far (`dd873bd7-…`, Torsion Spring SS304) has
`is_sellable: 0` and `default_bom_id: null`: no BOM at all. A full `Item.list`
read (2026-09-26) found a better candidate: **SuryaTools Bench Vice 150mm**
(`ST-VICE-150`, `bb7dd230-…`): manufactured, sellable, has a
`default_bom_id`, and it's the product in the "Nashik ITI bulk order, 60
vices" deal. Under today's rules it gets *quoted* from its list price, so
which outcome it demonstrates depends on #3.

**Status:** candidate found; the final choice waits on #3.

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

## Resolved

### Is `AgentMemory.create` worth keeping as the finding-storage step?

**Raised:** 2026-09-26. **Resolved:** 2026-09-26 — **kept.**

No purpose-built "submit your finding" tool exists, `/api/agent/evidence/*`
403s for this seat, and `AgentMessage` turned out to be read-only (no
`create`). So `AgentMemory.create` (`category: "context"`) remains the
platform copy of the finding. It's no longer the only record: every run also
saves `runs/<run_id>/taskrun.json` locally first, and a failed AgentMemory
write is only a warning.
