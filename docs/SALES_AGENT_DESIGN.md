# Sales agent — design decisions and progress

*Working notes from an iterative design conversation. Read this after `/clear` to
resume where we left off. Companion docs: `agent_build_handoff.md` (the brief),
`SALES_AGENT_PATH.md` (quote-to-cash recon), `how_to_file_bugs.md` (bug process).*

## The task

Build a **client agent** (a loop, not a server) for Team 6's Pipeline/Sales Agent
seat on AgentSwitch (company: Suryodaya Precision Works). It must answer, against
**live** data:

> "What closes this month, what is at risk, and quote 500 units off the real BOM price."

Full requirements are in `agent_build_handoff.md` — this file only captures the
decisions made *beyond* that brief, and the platform facts that shaped them.

## Definitions locked

- **At risk** = open deal (`stage` not `closed_won`/`closed_lost`) whose
  `expected_close_date` is in the past.
- **Closing this month** = open-stage deal (`new`/`qualification`/`proposal`/`negotiation`)
  whose `expected_close_date` falls in the current calendar month. Forecast
  reading — deliberately excludes deals that already closed `closed_won` this
  month (that's an "actuals" question, a different one).
- **Canonical stage list**: `new`, `qualification`, `proposal`, `negotiation`,
  `closed_won`, `closed_lost`. Not declared as an enum anywhere in the schema —
  reverse-engineered from `Deal.mark_won.<from>.<to>` / `Deal.mark_lost.<from>.<to>`
  transition tool names (see bug E2 below).
- **Escalate vs. refuse — one test, not two overlapping ones:**
  1. Is this legitimately the agent's job at all? **No** → refuse, file nothing.
     (payroll/commission questions, another rep's private data, a discount past
     policy, a deal/quote ID that doesn't exist.)
  2. **Yes** → can this seat do it directly, or does it need someone else's access?
     Seat can → just do it. Seat can't, but a human/another seat plausibly can →
     **escalate** (`AgentEscalation.create`), and tell the user plainly it can't
     produce the number itself, has escalated (with a reference), and won't guess.
  - The BOM-quote task is the **escalate** branch (quoting is legitimately this
    agent's job; manufacturing can answer it). It still satisfies the handoff's
    "refusal showcase" framing at the *content* level — the reply never invents a
    price — even though the *action* taken is escalate, not a bare refusal.
    **Not fully settled**: this may be exactly the capability the handoff
    expects us to solve outright (find a real price some other way), not
    route around via escalation. Keeping escalate as the BOM answer for now;
    revisit after instructor clarification.
  - **Pure-refuse example — LOCKED**: a nonexistent deal/quote ID (e.g. a
    garbage UUID passed to `get_deal`/`get_lead`). Chosen over the other three
    candidates (commission/payroll question, discount past policy, another
    rep's private data) because it's the only one that's clean to test
    end-to-end against live data with no invented policy thresholds and no
    dependency on whether this seat is even scoped per-rep (it likely isn't —
    `Deal.list` appears company-wide, so "another rep's private data" may not
    be a real refusal case on this platform at all). Behavior: not-found →
    refuse plainly, never guess/invent a deal, file nothing.

## Platform facts (from live probing — don't re-derive, re-verify if stale)

- MCP transport: plain JSON-RPC over `POST $SURYODAYA/api/mcp`, Bearer token from
  `POST /api/auth/login {email,password} -> {token}`. Credentials in `.env`
  (`EMAIL`, `SURYODAYA_PW`), loaded via `harness/env.sh`.
- The handoff's "Opportunities" = the `Deal` entity here (`Deal.list`, `Deal.get`,
  `Pipeline.list`/`Pipeline.get`).
- `_rot_level`/`_rot_days` on Deal records is **not** a usable risk signal — only
  ever `none`/`fresh` across a 100-row sample, always `none`/`0` on closed deals.
  Looked promising, ruled out after checking real data.
- **No server-side filter or aggregate exists** on `Deal.list` (pre-existing bug
  `a81bd641`: max 50/page, 137+ total deals, no stage/date filter, no group-by).
  Consequence: our domain code must **paginate every page itself and
  filter/sum in Python** — never let the LLM eyeball a partial page and report a
  total as if complete.
- **BOM wall is real and total, not a 403**: no `BOM.*` tool exists anywhere in
  the live 242-tool list. `Item` records carry `default_bom_id`/`design_bom_id`
  (bare unresolvable UUIDs) and `is_manufactured`/`routing_id`. Sales-visible
  price fields on Item (`default_rate`, `selling_price`, `standard_rate`,
  `purchase_rate`, `mrp`) are all `0.0` on real sampled data anyway — no fallback
  price exists even if we wanted to fudge one (we won't).
- Escalation mechanism: `AgentEscalation.create` (requires `session_id`, `reason`;
  optional `reason_code` enum, `subject`, `party_id`). Assignee confirmed via
  `GET /api/agent-governance/escalations/assignees` — the only entry is
  **Meera Kulkarni** (`meera.kulkarni@suryodaya.in`), also `created_by` on most
  seeded historical sessions.
- `POST /api/agent/chat` exists and is tied to our own seat's persona ("Sales
  Agent", seat_number 6) — **decided not to build on it**. It looks like
  AgentSwitch's own built-in chat product feature: session history under it is
  full of seeded/synthetic conversations (300-400+ messages, `total_tool_calls: 0`,
  `metadata: {"generated": true}`). If we built our agent as a wrapper around it,
  there'd be nothing left for us to actually build — the handoff wants the
  decision loop itself as the deliverable. Matches Team04's precedent (they
  treated AgentSwitch as tools-only, brought their own external LLM).
- `/api/agent/evidence/*` (looked like a natural home for a structured/gradable
  answer) belongs to the **accounting app**, not ours — 403'd with
  `"App 'accounting' is not enabled for your account"`. No native slot; we use
  `AgentMemory.create` ourselves instead.

## Bug candidates filed (in `bugs_candidates.json`, NOT yet submitted to the API)

Per `how_to_file_bugs.md`, candidates are handed off, not auto-filed. IDs E1-E3
were added this round:

- **E1** — `AgentEscalation.reason_code` enum has no value for a cross-app
  capability gap (the single most predictable escalation reason on a
  seat-scoped platform); has to be mis-filed under `other`.
- **E2** — `Deal.update`'s `stage` argument has no enum in its schema despite
  the API clearly enforcing a fixed set; only discoverable by reverse-engineering
  transition tool names.
- **E3** — the sales/Pipeline seat can write to `Item`, a manufacturing-owned
  entity (found via a leftover prior test record's own description field).
  Flagged as "confirm intent before filing as a bug" — may be deliberate (sales
  plausibly needs to create sellable items). **Instructor question drafted but
  not yet sent** — see chat log or ask to have it re-drafted.

Evidence for all three saved under `out/suryodaya/probes/`. A ready-to-paste
prompt for a fresh session to file E1/E2 (and ask before E3) was given in
conversation — regenerate it if needed by asking to file the E-series candidates.

## Architecture locked

- **Hand-rolled Python client loop**, three-layer split (mirrors Team04's
  graded prior submission, analyzed in the "Ideas for Harness" doc —
  `https://claude.ai/artifact/WVrWLdxaquYY82kPejt9k1`):
  `LLM ↔ agent.py (the loop) ↔ domain.py (real logic, zero LLM, unit-testable) ↔ MCP/REST`.
- **Curated tool menu** for the LLM — not raw MCP passthrough. Deliberately
  excludes every write/transition/bypass tool (e.g. `Deal.mark_lost.new.closed_lost`,
  `Quotation.convert_to_order.*`) from the LLM's reach entirely, by construction:
  1. `list_closing_this_month()` — paginated `Deal.list` + forecast filter
  2. `list_at_risk()` — paginated `Deal.list` + overdue-open filter
  3. `attempt_quote(...)` — tries the BOM-dependent path, returns the escalate outcome
  4. `file_escalation(reason, ...)` — wraps `AgentEscalation.create`
  5. `get_deal(id)` — generic read (flexibility valve)
  6. `get_lead(id)` — generic read (flexibility valve)
- **LLM calls: via `glc_v5`**, the user's own local LLM gateway
  (`/Users/sagarshete/Documents/eagv3/glc_v5`, unrelated repo). Confirmed:
  `POST http://127.0.0.1:8111/v1/chat` takes OpenAI-style `messages` + `tools` +
  `tool_choice`, returns canonical `tool_calls` + `stop_reason`
  (`tool_use`/`end_turn`/...), normalizes function-calling across providers,
  and gives cost/budget/retry handling for free. **Operational dependency**:
  it's a local server process (`uv run glc serve`, port 8111) that must be
  running alongside our agent script — not a hosted endpoint. The Gemini key
  lives in `glc_v5/.env`, not this repo's `.env`; our agent needs no LLM key of
  its own, just the base URL.
- **Structured finding schema** (the `record_finding` equivalent — a gradable
  object separate from whatever English the LLM writes) — **FINAL**:
  ```json
  {
    "closing_this_month": {
      "outcome": "answered",
      "month": "2026-09",
      "deal_ids": ["..."],
      "total_value": 12345.0,
      "pagination_complete": true
    },
    "at_risk": {
      "outcome": "answered",
      "rule": "open deal with expected_close_date in the past",
      "deal_ids": ["..."],
      "reasons": {"<deal_id>": "expected_close_date 2026-01-05 passed, still in stage 'new'"},
      "pagination_complete": true
    },
    "quote": {
      "outcome": "escalated",
      "requested_qty": 500,
      "item_id": "...",
      "reason": "no BOM.* tool available to this seat; sales-visible price fields are unset",
      "escalation_id": "..."
    },
    "generated_at": "<server clock time, not local>"
  }
  ```
  Every section shares one common sub-shape (`outcome`, `deal_ids`/`item_id`,
  `pagination_complete` where applicable, `reason`), but the **legal `outcome`
  values differ per section** because the task shapes differ:
  - `closing_this_month.outcome`, `at_risk.outcome`: `answered` | `error`.
    These are plain deterministic reads (paginate `Deal.list`, filter,
    return) with no escalate/refuse branch — the only failure mode is the
    underlying call erroring outright, distinct from `pagination_complete:
    false` (partial data, call itself succeeded).
  - `quote.outcome`: `quoted` | `escalated` | `refused` | `error`. This one
    *does* branch (the escalate-vs-refuse decision tree above), so it needs
    the full set. **None of these four rank above another** — a grader checks
    "did it pick the outcome that matches ground truth for this request," not
    "did it reach `quoted`." `quoted` is kept in the enum for when/if the BOM
    wall gets fixed, not because it's the target outcome today; on current
    live data the only correct value for the BOM-quote task is `escalated`.
  `pagination_complete` directly counters bug `a81bd641`'s "partial sum
  indistinguishable from complete" risk. Only `deal_ids`/`outcome` get
  graded, never prose — a verifier re-derives the same lists live and diffs.
  Storage target: `AgentMemory.create` (`category: "context"`, `content` = this
  JSON as a string, tagged with our own `session_id`).

## Not yet done — pick up here

1. ~~Finalize the finding schema~~ — **done**, see FINAL schema above.
2. ~~Lock a pure-refuse example~~ — **done**, nonexistent deal/quote ID, see
   above. (BOM-as-escalate is still open pending instructor input — not the
   same thing as this item.)
3. **Code written — `mcp_client.py`, `llm_client.py`, `domain.py`, `tools.py`,
   `agent.py` all exist at repo root.** Compiles clean, imports clean, and
   `domain.py`'s pure logic (pagination-across-pages, both filters,
   attempt_quote's quoted/escalated/refused branches, get_deal/get_lead's
   refuse-on-not-found) is verified against fake-client unit tests — not yet
   run against live Suryodaya data or a live `glc_v5`. Still open:
   - `agent.py`'s tool-loop (`run()`) itself is untested — only the domain
     functions it calls have been exercised directly.
   - `generated_at` uses local UTC clock, not a real server-clock source (none
     was found) — noted as a known gap, not silently fixed.
4. ~~Send instructor question about E3; file E1/E2~~ — **done, stale item**.
   Per `BUGS_FILED.md`'s "Session of 2026-09-23": E1 filed as `4e90fc79`, E2
   filed as `87d456e5` (with a correction to E2's premise before filing — no
   `stage` value is accepted, not just an undeclared enum). E3 was correctly
   **not** filed: it duplicated a report already on file from 2026-09-17 (E3's
   own "evidence" was a leftover test record created by that earlier probe).
   The instructor question about E3 is therefore moot — nothing new to ask.
5. Run end-to-end against live Suryodaya data + a live `glc_v5` (handoff's
   definition of done) — start `uv run glc serve` in `glc_v5`, then
   `python3 agent.py --item-id <a real Item.id> --qty 500`.
6. ~~Write the README~~ — **done**, see `README.md` at repo root: risk
   definition, escalation path, refusal conditions, plus the operational
   gotchas found during the live run (provider pinning, Gemini schema
   quirk, the `session_id` FK).
