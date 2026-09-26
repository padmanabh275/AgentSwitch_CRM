# Sales / Pipeline agent — Team 6, seat 6

A client agent (a loop, not a server) for Suryodaya Precision Works. It
answers, against **live** data:

> "What closes this month, what is at risk, and quote 500 units off the real
> BOM price."

Full design rationale and platform facts: `SALES_AGENT_DESIGN.md`. Open
questions not yet settled: `TO_REVISIT.md`.

## Running it

Needs `glc_v5` (a separate repo — the LLM gateway) running locally, and
`EMAIL`/`SURYODAYA_PW` in `.env` at this repo's root.

```bash
cd ../glc_v5 && uv run glc serve &   # or wherever glc_v5 lives; port 8111
cd -
python3 agent.py --item-id <a real Item.id> --qty 500
```

`--item-id` is optional — omit it to skip the quote question and answer only
"closing this month" / "at risk". `--query` overrides the composite question
entirely, for testing a single tool in isolation (e.g. a get_deal refusal).

## Architecture

```
LLM  <->  agent.py (the loop)  <->  domain.py (real logic, zero LLM)  <->  MCP/REST
```

The LLM only ever sees a **curated menu of six tools** (`tools.py`) — never
raw MCP passthrough. Every write/transition/bypass tool on this seat
(`Deal.mark_lost.*`, `Quotation.convert_to_order.*`, etc.) is excluded from
the LLM's reach by construction, not by prompting:

| Tool | What it does |
|---|---|
| `list_closing_this_month` | Paginated `Deal.list` + forecast filter |
| `list_at_risk` | Paginated `Deal.list` + overdue-open filter |
| `attempt_quote` | Tries the BOM-dependent price path; escalates when it can't |
| `file_escalation` | Wraps `AgentEscalation.create` |
| `get_deal` / `get_lead` | Generic single-record reads (flexibility valve) |

Each tool call produces one section of a **structured finding** — a
gradable JSON object, separate from whatever prose the LLM writes — printed
to stdout and (best-effort) persisted via `AgentMemory.create`. Only the
finding's `deal_ids`/`outcome` fields are meant to be graded; a verifier
re-derives the same lists live and diffs, never trusts the prose.

## Risk definition

- **"Closing this month"** = an open-stage deal (`new`/`qualification`/
  `proposal`/`negotiation`) whose `expected_close_date` falls in the current
  calendar month. Deliberately excludes deals already `closed_won` this
  month — that's an "actuals" question, a different one.
- **"At risk"** = an open deal (any stage except `closed_won`/`closed_lost`)
  whose `expected_close_date` is already in the past. A deal can be in both
  lists at once — "due this month" and "already overdue" aren't mutually
  exclusive, and the agent doesn't force them to be.
- `_rot_level`/`_rot_days` (a field that looked like a ready-made staleness
  signal) was tried and ruled out — only ever `none`/`fresh` across a
  100-row live sample.
- Both lists paginate `Deal.list` themselves and filter in Python, because
  the platform has no server-side filter or aggregate on it (bug
  `a81bd641`: the schema advertises `limit<=1000` but the server silently
  caps real pages at 50). A partial sum would otherwise be indistinguishable
  from a complete one — every list result carries `pagination_complete` to
  make that visible.

## Escalation path

`file_escalation` wraps `AgentEscalation.create`. The only registered
assignee on this platform is **Meera Kulkarni**
(`meera.kulkarni@suryodaya.in`) — confirmed via
`GET /api/agent-governance/escalations/assignees`.

`reason_code` defaults to `"other"`: the enum
(`unresolved_after_retries`/`customer_asked_for_a_person`/`policy_refusal`/
`sensitive_topic`/`agent_error`/`other`) has no value for "my seat can't
reach this app" — the single most predictable escalation reason on a
seat-scoped platform (bug candidate E1, filed as `4e90fc79`).

**When the agent escalates:** it's legitimately the agent's job, it can't do
it directly, but a human or another seat plausibly can. The concrete case
built into this agent: quoting a price. No `BOM.*` tool exists anywhere in
this seat's tool catalog (confirmed against the live 242-tool list, not
just an empty result), and every sales-visible price field on `Item`
(`default_rate`, `selling_price`, `standard_rate`, `purchase_rate`, `mrp`)
is `0.0` on real live data. `attempt_quote` checks for a real price first
and only escalates when none exists — verified live: escalating never
produces a fake number, and the filed escalation (`ESC-2026-00026` in
testing) is independently readable back via `AgentEscalation.get`.

## Refusal conditions

Escalate-vs-refuse is one test, not two overlapping ones:

1. **Is this legitimately the agent's job at all?** If not — refuse, file
   nothing. Examples: a payroll/commission question, another rep's private
   data, a discount past policy, an id that turns out not to exist.
2. **If yes**, and the seat can't do it directly but a human/another seat
   plausibly can → escalate (see above).

**The locked refusal showcase:** `get_deal`/`get_lead` called with a
nonexistent id. Both call the real MCP tool (`Deal.get`/`Lead.get`); a
not-found response is reported back plainly — `outcome: "refused"` — and
nothing is invented, nothing is filed. This was chosen over the other
candidates (commission question, discount policy, another rep's data)
because it's the only one testable end-to-end with no invented policy
threshold and no dependency on whether this seat is even scoped per-rep (it
likely isn't — `Deal.list` appears company-wide).

## Known operational gotchas (see `SALES_AGENT_DESIGN.md` / `TO_REVISIT.md`)

- The LLM gateway (`glc_v5`) must be pinned to a provider with native
  tool-calling. Left un-pinned, it defaults to a local Ollama model whose
  `tool_call_dialect` is `prompted_fallback` — it narrates tool calls as
  text instead of making them, and nothing gets dispatched.
- Gemini's function-calling schema rejects `additionalProperties` — plain
  JSON Schema, but outside Gemini's OpenAPI-subset dialect. Tool specs in
  `tools.py` deliberately omit it.
- `session_id` on `AgentMemory.create`/`AgentEscalation.create` is a hidden
  foreign key to a real `AgentSession` row, not a free-form string — the
  schema gives no indication of this (candidate bug F1, not yet filed).
  `agent.py` calls `AgentSession.create` at startup and uses its id
  everywhere `session_id` is required.
