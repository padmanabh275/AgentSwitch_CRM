# Sales agent — design decisions and progress

*Working notes from an iterative design conversation. Read this after `/clear` to
resume where we left off. Companion docs (all in `docs/`): `agent_build_handoff.md`
(the brief), `SALES_AGENT_PATH.md` (quote-to-cash recon), `UNHAPPY_PATHS.md`
(error-code recon), `TO_REVISIT.md` (open decisions). Raw probe evidence
(`out/suryodaya/...`), the recon harness and `how_to_file_bugs.md` live in the
capstone working folder, outside this repo.*

## The task

Build a **client agent** (a loop, not a server) for Team 6's Pipeline/Sales Agent
seat on AgentSwitch (company: Suryodaya Precision Works). It must answer, against
**live** data:

> "What closes this month, what is at risk, and quote 500 units off the real BOM price."

Full requirements are in `agent_build_handoff.md` — this file only captures the
decisions made *beyond* that brief, and the platform facts that shaped them.

## Definitions locked

- **At risk** = open deal (`stage` in `new`/`qualification`/`proposal`/`negotiation`)
  whose `expected_close_date` is in the past. Open deals with *no* close date
  can't be judged by this rule and are reported separately
  (`open_without_close_date_ids`) — 69 of 88 open deals on 2026-09-26, so
  whether they should count is an open decision (`TO_REVISIT.md`).
- **"Open" is one shared definition** (`domain.deals.is_open`): the four
  forecast stages. A stage outside the known six counts as neither open nor
  closed and is surfaced as `unknown_stage_ids`, not silently included.
- **Dates are judged on the IST calendar** (Suryodaya is in India; UTC's
  "today" is a day behind between 00:00 and 05:30 IST).
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
  `POST /api/auth/login {email,password} -> {token}`. Credentials in `agent/.env`
  (`EMAIL`, `SURYODAYA_PW`), loaded by `agent/config.py`.
- The handoff's "Opportunities" = the `Deal` entity here (`Deal.list`, `Deal.get`,
  `Pipeline.list`/`Pipeline.get`).
- `_rot_level`/`_rot_days` on Deal records is **not** a usable risk signal — only
  ever `none`/`fresh` across a 100-row sample, always `none`/`0` on closed deals.
  Looked promising, ruled out after checking real data.
- **No server-side aggregate or date-range filter exists** on `Deal.list`
  (pre-existing bug `a81bd641`: max 50/page, 138 total deals on 2026-09-26, no
  group-by). Exact-match filters *do* work — `stage: "proposal"` returned only
  proposal deals (verified 2026-09-26) — but nothing can express "close date
  in this month" or "before today". Consequence: our domain code must
  **paginate every page itself and filter/sum in Python** — never let the LLM
  eyeball a partial page and report a total as if complete.
- **BOM wall is real and total, not a 403**: no `BOM.*` tool exists anywhere in
  the live 242-tool list. `Item` records carry `default_bom_id`/`design_bom_id`
  (bare unresolvable UUIDs) and `is_manufactured`/`routing_id`.
  - ~~Sales-visible price fields are all `0.0` on real sampled data~~ —
    **wrong beyond that sample.** A full read on 2026-09-26: **94 of 103 items**
    have a non-zero price field (`default_rate` 87, `purchase_rate` 88,
    `selling_price` 42, `mrp` 40, `standard_rate` 27); 69 have a
    `default_bom_id`. E.g. SuryaTools Bench Vice 150mm (`ST-VICE-150`,
    manufactured, has a BOM): `default_rate`/`selling_price` 5799, `mrp` 6999,
    `purchase_rate` 3189.45. So for most items the agent *quotes* a list price
    rather than escalating — see `TO_REVISIT.md` #3. `purchase_rate` is a
    cost and is never quoted.
  - `Item.list`'s `search` matches name and code, case-insensitively
    ("bench vice" → 2 items, "ASP-H-011" → 1).
- Escalation mechanism: `AgentEscalation.create` (requires `session_id`, `reason`;
  optional `reason_code` enum, `subject`, `party_id`). Assignee confirmed via
  `GET /api/agent-governance/escalations/assignees` — the only entry is
  **Meera Kulkarni** (`meera.kulkarni@suryodaya.in`), also `created_by` on most
  seeded historical sessions.
  - **But our own escalation came back unassigned** (checked 2026-09-26):
    `ESC-2026-00026` has `assignee_user_id`, `assignee_display` and
    `raised_at` all null, while Team 04's `ESC-2026-00025` shows
    `assignee_display: "Meera Kulkarni"`, `channel: "api"`, `sla_minutes: 240`.
    Something about how they file differs. The agent now reports the assignee
    read back from the record, never a hard-coded name. Open item in
    `TO_REVISIT.md`.
  - `AgentEscalation.list`'s `subject` filter is **exact-match** (a prefix
    returns nothing; `search` does substring). Observed statuses: `open`,
    `withdrawn`. This is what makes escalation idempotent: before filing, the
    agent looks for an open escalation with the same `T6-...` subject.
- Error envelope and codes: see `UNHAPPY_PATHS.md`. Always HTTP 200 with
  `error.data.code` in `not_found` / `invalid_arguments` / `invalid_transition` /
  `tool_not_available`. A malformed id (`Item.get {id:"not-a-uuid"}`) returns
  `not_found`, not `invalid_arguments` (re-verified 2026-09-26).
- **`AgentMessage` is read-only for this seat** — `list`/`get` only, no
  `create`. The run transcript therefore lives in local run records, not on
  the platform. `AgentTask` does have `create`/`pause`/`resume`/`complete`
  (the planned mechanism for waiting on an escalation reply), and
  `AgentSession.update` accepts `total_tool_calls` and a string `metadata`.
- The item used in testing so far, `dd873bd7-…` (Torsion Spring SS304,
  `ASP-H-011`), has `is_sellable: 0` and `default_bom_id: null` — no BOM
  exists for it at all, so it's a weak example for a "BOM price" demo. Pick
  a manufactured item with a `default_bom_id`.
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

## Bug candidates E1–E3

**Status (2026-09-23, see `BUGS_FILED.md`):** E1 filed as `4e90fc79`; E2 filed
as `87d456e5` (premise corrected before filing — no `stage` value is accepted
by `Deal.update` at all, not just an undeclared enum); E3 **not** filed — it
duplicated a report already on file from 2026-09-17. The notes below are the
original candidate write-ups.

- **E1** — `AgentEscalation.reason_code` enum has no value for a cross-app
  capability gap (the single most predictable escalation reason on a
  seat-scoped platform); has to be mis-filed under `other`.
- **E2** — `Deal.update`'s `stage` argument has no enum in its schema despite
  the API clearly enforcing a fixed set; only discoverable by reverse-engineering
  transition tool names.
- **E3** — the sales/Pipeline seat can write to `Item`, a manufacturing-owned
  entity (found via a leftover prior test record's own description field).
  Flagged as "confirm intent before filing as a bug" — may be deliberate (sales
  plausibly needs to create sellable items). (Moot — see status above.)

Evidence for all three is under `out/suryodaya/probes/` in the capstone
working folder (not in this repo).

## Architecture

**Revised 2026-09-26 (Phase 1): the composite question runs as a task graph.**
The original design (below) had the LLM drive a tool loop and the finding was
assembled from whichever tools it happened to call — so a gradable section
could silently go missing. A review found that, plus errors all being read as
"not found", two separate deal reads per run, and no run log. The fix, ported
from `designreview/core/dag_engine.py` (Team 21's port of S17's live graph)
and S18's harness pattern:

- `domain/` stays the zero-LLM core. `transport/` raises typed `MCPToolError`s.
- `graph/`: nodes end with a `NodeOutcome` (answered / refused / escalated /
  deferred / error / skipped). Each node declares `needs` — `"answered"` (skip
  if a dependency didn't answer) or `"done"` (the partial join: run once
  everything finished, however it ended). Nodes name entries in
  `graph/registry.py` — the allowlist — never raw MCP tools. `RulePlanner` adds
  `escalate_quote` when the item is unpriced. Stdlib-only and sequential:
  networkx and threads bought nothing at ~8 nodes.
- `build_finding` is a graph node: the finding is computed in code on every
  run, independent of the LLM. The LLM only narrates it (`llm/narrate.py`),
  with a deterministic template fallback.
- `harness/`: an S18-style `TaskRun` saved to `runs/<run_id>/` *before* any
  platform write; `graph.json` beside it holds every node's output.
- The LLM tool loop is kept as `run.py --chat` for free-form questions, still
  behind the curated menu below.
- **Phase 2 (2026-09-26):** a free-text question is classified by the LLM
  (`llm/intent.py`, JSON schema via glc_v5's `response_format`) and validated
  in code — no default quantity, unknown asks dropped. Standard questions and
  out-of-scope refusals go to the graph; other in-scope requests go to the
  chat loop. `resolve_item` finds items by id/code/name and refuses on no or
  ambiguous matches. `llm/verify.py` checks every answer against the finding
  (ids, escalation numbers, amounts within ₹1, required facts); one rewrite,
  then the template. `RulePlanner` re-reads an incomplete snapshot once.
- Roadmap: Phase 3 (wait for escalation replies and resume, an LLM planner
  limited to the registry, a richer at-risk rule). See `README.md` → Status.

**Original design (2026-09-2x), still the basis of the `--chat` path:**

- **Hand-rolled Python client loop**, three-layer split (mirrors Team04's
  graded prior submission, analyzed in the "Ideas for Harness" doc —
  `https://claude.ai/artifact/WVrWLdxaquYY82kPejt9k1`):
  `LLM ↔ agent.py (the loop) ↔ domain.py (real logic, zero LLM, unit-testable) ↔ MCP/REST`.
  (Now `llm/chat_loop.py` ↔ `domain/`.)
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

  **Additions in Phase 1 (2026-09-26) — additive; nothing above was removed:**
  - Every requested section is always present. Sections gained two outcomes:
    `skipped` (a dependency didn't answer; carries `node` and `reason`) and
    `not_requested` (excluded via `--asks`). `error` sections carry `node` and
    `error_code`.
  - `closing_this_month` / `at_risk`: `deals` (per-deal summaries: title,
    value, stage, dates, owner; `days_overdue` for at-risk), `total_by_currency`
    (`total_value` is null if currencies are mixed), `snapshot_at`. `at_risk`
    also has `as_of`, `total_value` and `open_without_close_date_ids`.
  - `quote`: `price_source` when quoted (it's the Item's list price, not a BOM
    cost); `number`, `status`, `assignee`, `reused_existing` when escalated;
    `dry_run` and `would_file` under `--dry-run`. `outcome: "refused"` also
    covers "no item specified" and a non-positive quantity.
  - **Phase 2:** `quote.outcome: "refused"` also covers an unstated quantity,
    no matching item, and an ambiguous item (with `candidates`); resolved
    quotes carry `item_name`, `item_code`, `query`, `matched_by`. Top level
    gains `refusals` (out-of-scope parts, each `{outcome, request, reason}`)
    and `not_handled` (`{request, reason}` or null) — always present.
  - Top level: `run_id`, `session_id`, `dry_run`. `generated_at` is the local
    clock in IST — still no server-clock source.

## Progress

- **2026-09-26 — Phase 1 done** (branch `feature/sales-pipeline-agent-loop`):
  the task graph, typed errors, one deal snapshot per run, idempotent T6-
  escalations, run records, `--dry-run`, `--today`, pytest via uv. Verified
  against live Suryodaya data in dry runs (graph path and the `--chat`
  refusal) and offline with fake clients. **The path that writes to the
  platform (session, escalation, memory) has not been run yet.**
- **2026-09-26 — Phase 2 done:** free-text routing, item lookup by name,
  prose verification, out-of-scope refusals, snapshot re-read. Live dry runs:
  the composite question by item name, a mixed question (quote without a
  quantity refused, commission refused), and a deal lookup routed to chat.
- Earlier checklist items (finding schema, pure-refuse example, E1/E2 filing,
  README) are all done — see git history for the old list.
- Next: Phase 2/3 per `README.md` → Status; open decisions in `TO_REVISIT.md`.
