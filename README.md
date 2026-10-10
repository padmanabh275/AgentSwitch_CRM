# Sales / Pipeline agent — Team 6, seat 6

A client agent (it calls AgentSwitch; it isn't a server) for Suryodaya Precision
Works. It answers, against **live** data:

> "What closes this month, what is at risk, and quote 500 units off the real
> BOM price."

Design rationale and platform facts: [`docs/SALES_AGENT_DESIGN.md`](docs/SALES_AGENT_DESIGN.md).
Open decisions: [`docs/TO_REVISIT.md`](docs/TO_REVISIT.md).

## Quick start

```bash
uv sync                                   # Python >=3.10; the agent itself is stdlib-only
printf 'EMAIL=...\nSURYODAYA_PW=...\n' > agent/.env   # your seat login (gitignored)
(cd path/to/glc_v5 && uv run glc serve &) # LLM gateway, port 8111 — see below

uv run python agent/run.py "What closes this month, what's at risk, and quote 500 Toolmaker's Vice 105mm (Mtr)?" --dry-run
uv run python agent/run.py --item "SP2319/3567 #3567" --qty 500 --dry-run   # same, without LLM classification
```

A free-text question is classified by the LLM (`llm/intent.py`) into the three
standard questions, the item and quantity as written, out-of-scope parts, and
anything else. The first three go to the task graph; out-of-scope parts are
refused there; any other in-scope request goes to the chat loop. Running with
flags only skips the classification, which makes it the reproducible path for
grading.

| Flag | Effect |
|---|---|
| `"question"` | Free-text question, classified and routed as above. |
| `--item ITEM`, `--qty N` | Flags path: the item to quote (id, code or name; `--item-id` is an alias) and quantity (default 500). Without `--item` the quote is refused ("which item?"). |
| `--asks a,b,c` | Subset of `closing_this_month,at_risk,quote` (default: all three). |
| `--today YYYY-MM-DD` | Pin the date "this month" and "overdue" are judged against. Deal data is still live. |
| `--dry-run` | Read live data, **write nothing**: no session, escalation or memory. |
| `--chat "question"` | Straight to the LLM tool loop, skipping classification. |

Every run writes `runs/<run_id>/taskrun.json` (steps, finding, answer,
warnings; saved first with `ended: "running"`, so a crash still leaves a
record), `calls.jsonl` (every MCP call as the transport saw it) and either
`graph.json` (every node's full output, including the deal snapshot) or, for
the chat path, `chat_trace.json` (each tool's arguments and full result).
`runs/` is gitignored.

Credentials come from `agent/.env`, then the repo's `.env`: `EMAIL` +
`SURYODAYA_PW` (password login, renewed on a 401), or a bearer `TOKEN`
(used as is; `AS` overrides the base URL). On the hosted harness run,
`AGENTSWITCH_TOKEN` + `AGENTSWITCH_BASE_URL` take precedence over all of these.

**LLM gateway.** LLM calls go through `glc_v5`, a separate local repo (`GLC_URL`, default `http://127.0.0.1:8111`). It's pinned to the `gemini`
provider (`GLC_PROVIDER`): unpinned, it picks a local Ollama model that writes
tool calls out as text instead of making them. If the gateway is down, the
graph path still answers, using a plain template instead of LLM prose; `--chat`
can't run without it.

**OpenAI-compatible model.** If `OPENAI_BASE_URL` or `OPENAI_API_KEY` is set,
LLM calls go to `$OPENAI_BASE_URL/chat/completions` with `OPENAI_MODEL`
instead of glc_v5 — what the hosted harness run provides. Callers don't
change: `transport/llm_client.py` translates tools, tool calls and replies.
Locally any OpenAI-compatible server works, e.g. Ollama with a tool-capable
model (`OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama
OPENAI_MODEL=gemma4:e4b`).

## How it works

```
                 run.py
              │  a free-text question is first classified (llm/intent.py)
       ┌───────────┴───────────┐
   graph path                chat path (or --chat)
       │                       │
  graph/ (task graph)     llm/chat_loop.py (LLM picks tools from llm/tools.py)
       │                       │
       └──────── domain/ (all the real logic, no LLM) ──── transport/ ──── AgentSwitch MCP
```

The composite question runs as a small task graph. Only the asked-for branches
are built:

```
snapshot_deals (+ reread_deals) ─┬─ closing_this_month ─┐
                                 └─ at_risk ────────────┤
resolve_item ── price_lookup ──(+ escalate_quote)───────┤
refuse_1..n (out-of-scope parts) ───────────────────────┤
                                         build_finding ── narrate (verified)
```

- **The finding is computed in code, not by the LLM.** `build_finding` assembles
  every requested section on every run: answered, refused, escalated, error,
  skipped (with the reason), or not_requested. The LLM only turns the finding
  into prose (`narrate`).
- **One failed branch doesn't sink the others.** `build_finding` runs once every
  branch has finished, whatever state each ended in. A node whose input didn't
  come through is marked `skipped` with the reason, rather than running on bad data.
- **The graph extends itself.** `RulePlanner` adds `escalate_quote` when the item
  has no BOM price, and one full `reread_deals` when the snapshot came back
  incomplete; the nodes that need the new result wait for it.
- **The prose is checked against the finding.** `llm/verify.py` rejects an
  answer that states an id, escalation number or amount not in the finding, or
  leaves out a required fact (section totals, undated-deal count, an
  escalation or dry run). A failing answer gets one rewrite listing the
  problems, then the plain template is used instead.
- **Nodes can only do what `graph/registry.py` allows.** Nodes name registry
  entries, never raw MCP tools, so no plan or planner can reach
  `Deal.mark_lost.*`, `Quotation.convert_to_order.*` or any other write outside
  it. The only write in the registry is `escalate_quote`.
- **Errors keep their code.** `transport/mcp_client.py` raises `MCPToolError` with
  the platform's `error.data.code` (`not_found`, `invalid_arguments`,
  `tool_not_available`, ...) or a transport code (`transient`, `auth`,
  `forbidden`). Only `not_found` becomes a refusal; transient failures are
  retried on read nodes (never on writes); an expired login is renewed once.

## Definitions

- **Closing this month**: an open deal (`new`/`qualification`/`proposal`/`negotiation`)
  whose `expected_close_date` falls in the current month on the **IST** calendar.
  Deals already `closed_won` this month are excluded: that's a question about
  actuals, not the forecast.
- **At risk**: an open deal whose `expected_close_date` has already passed.
  Each flagged deal carries its reason and days overdue, largest value first.
  A deal can be in both lists (due this month *and* already overdue).
  - Open deals with **no** close date can't be judged by this rule. They're
    listed separately as `open_without_close_date_ids`. On 2026-09-26 that was
    69 of 88 open deals; see `docs/TO_REVISIT.md`.
  - `_rot_level`/`_rot_days` looked like a ready-made risk signal but was ruled
    out: only ever `none`/`fresh` across a 100-row live sample.
- **Both lists come from one read** (`snapshot_deals`) of every deal, so they
  describe the same moment in a book Team 07 also writes to. `Deal.list`
  silently caps pages at 50 (bug `a81bd641`), so the agent pages through itself.
  `pagination_complete` is `false` if the server omits `total` or rows shift
  during the read, so a partial list never passes as complete.

## Escalation and refusal

One test decides between them:

1. **Is this legitimately the agent's job?** If not, refuse and file nothing.
   Examples: payroll/commission, a discount past policy, an id that doesn't exist.
2. **If it is**, but this seat can't do it and a person or another seat can,
   escalate (`AgentEscalation.create`) and say so. Never guess a substitute answer.

**The quote is answered, not escalated, whenever a BOM price exists.** The
BOM price is `Item.standard_rate`. This seat has no `BOM.*` tool, but it doesn't
need one: `standard_rate` is a read-only field on `Item` (in its
`_readonly_fields`, next to `default_bom_id` and `routing_id`), and on live
data it is set on 27 items, all of which have a BOM, and never on an item
without one. So it's manufacturing's BOM costing, readable from an entity this
seat owns. (Instructor to confirm this is the intended field.)
- The item is looked up by id, code or name. No match or several matches are
  refused ("which one?"), and so is a quote with no stated quantity.
- **With a BOM price:** the quote is `standard_rate` × qty, before tax and
  discounts (`price_source: Item.standard_rate`, plus `price_as_of`). E.g.
  Toolmaker's Vice 105mm (Mtr) has a BOM price of ₹6,02,246.98.
- **Without one:** 42 of the 69 items with a BOM have no `standard_rate` yet
  (e.g. SuryaTools Bench Vice 150mm), and items with no BOM have none at all.
  The quote is escalated. List prices (`default_rate`, `selling_price`, `mrp`)
  are shown for context but are never quoted in place of the BOM price, and
  `purchase_rate` (a cost) is never used.
- **The escalation:**
  - The subject is `T6-BOM quote for <qty>x <item_id>`.
  - If an **open** escalation with that exact subject already exists, it's
    reused rather than filing a duplicate.
  - `reason_code` is `other`: the enum has no value for "my seat can't reach
    that app" (bug `4e90fc79`).
  - ⚠️ Escalations filed from this seat so far show **no assignee**
    (`ESC-2026-00026`: `assignee_user_id: null`). Don't assume one reaches
    Meera Kulkarni until that's resolved; see `docs/TO_REVISIT.md`.

**The refusal showcase** is a nonexistent id: `--chat "Look up deal <random uuid>"`,
or `--item <random uuid>` for the quote. The agent refuses plainly, invents
nothing and files nothing. Out-of-scope parts of a free-text question
(commission, payroll, ...) are refused too, and listed under `refusals` in the
finding. It refuses only when the platform actually says
`not_found`: an auth or network error is reported as an error, not as "doesn't exist".

## Writes to shared data

Without `--dry-run`, a run writes to the team's own agent space on AgentSwitch:
an `AgentSession` titled `T6-Sales pipeline agent run`, at most one escalation
(or reuses an open one), an `AgentMemory` holding the finding, and an
`AgentSession.update` with the tool-call count and run id. The local run record
is saved **before** any of these, and a failed platform write becomes a warning,
never a lost run. There's no `AgentMessage.create` tool for this seat, so the
full trace lives in `runs/`.

## Harness

Its own loop, a task set with verifiers that read the platform rather than
the agent's prose, and every run written to disk before anything is scored.

```bash
cd agent
uv run python -m harness.runner                 # all tasks, every run --dry-run
uv run python -m harness.runner --tasks at_risk,quote_no_bom --skip-llm
uv run python -m harness.score ../runs/batches/<batch_id>   # files only, re-runnable
```

**Run, then score, never both at once.** For each task the runner:

1. reads the platform state the task's verifiers need, with its **own**
   login → `ground_truth_before.json`;
2. runs the agent exactly as a person would, `run.py <argv> --today <pinned>
   --run-id ... --dry-run`, in a subprocess → `taskrun.json`, `calls.jsonl`,
   `graph.json` / `chat_trace.json`, `stdout.txt`;
3. reads the platform again → `ground_truth_after.json`;

and records the batch in `runs/batches/<batch_id>/manifest.json`. `score.py`
then reads only those files and writes `score.json` per run and `report.md`
per batch. A scorer fix is applied by re-scoring saved runs, not by paying
for new live ones. `--write` lets runs write to the platform (off by
default); tasks needing the LLM are skipped when the gateway is down.

**Task set** (`agent/harness/tasks.json`, hand-editable): each task has an
`id`, the `argv` passed to `run.py`, `requires_llm`, `expect` (per-section
outcomes, `refusals_min`, or `chat_tools` outcomes) and `verifiers`. It covers
closing this month, at risk, the composite question, the escalated quote
outcomes (with and without a BOM), four refusals
(nonexistent item, ambiguous item, no item, nonexistent deal via chat), and
four refusals from the week-one gaps (invoice, email, merge, commission),
where refusal is correct because the platform has no tool for it. The
composite task no longer pins its quote to `quoted`: its item's
`standard_rate` was cleared on the shared instance, so `quote_matches_db`
decides the right outcome from live data, and no task currently guarantees
the quoted path.

**Verifiers** (`agent/harness/verifiers.py`): the rules are re-derived from
raw rows, not imported from `domain/`, and the agent's own snapshot is never
ground truth, so a bug in the agent can't pass its own check.

| Verifier | Checks against the platform |
|---|---|
| `deal_set_matches_db` | deal ids and per-currency total vs the rule applied to `Deal.list`; at-risk reasons and `days_overdue` |
| `quote_matches_db` | the outcome the item's data calls for (`standard_rate` set → quoted, unset → escalated, missing/ambiguous → refused); total = `standard_rate` × qty; no price on an escalation or refusal |
| `escalation_effect` | a claimed escalation exists (or was reused, or in a dry run only `would_file`s) with the `T6-BOM quote for …` subject; nothing filed otherwise |
| `record_absent` | the id really is `not_found`, and the chat trace refused it |
| `tools_absent` | `tools/list` has no tool for the request, and none was called |
| every task | the run ended `done`; `expect` holds; `calls.jsonl` holds only reads (plus the four allowed writes in write mode); T6 session and escalation counts didn't grow in a dry run |

Check statuses are `pass`, `fail`, `drift`, `skip` or `error`. **Drift:** the
book is shared with Team 07, so an id in only one of the two sets is `drift`,
not `fail`, when the deal moved during the run (classified differently before
and after, gone, or `updated_at` later than the agent's `snapshot_at`).
`error` means ground truth couldn't be read (e.g. an expired token) or the
task's premise doesn't hold on live data; it counts as not passing. A task
fails on any `fail` or `error`, is `drift` if drift is the only problem, and
otherwise passes.

**Hosted run** ("Our harness" → Submit for a run). `agentswitch-harness.toml`
tells the platform to `pip install -r requirements.txt` (just `certifi`,
whose CA bundle `transport/tls.py` uses if present; the agent is stdlib only) and run `python agent/run_hosted.py` from the repo root against
a fresh copy of each listed instance, with `AGENTSWITCH_*` and `OPENAI_*` set
and no internet. `harness/hosted.py` runs the batch, scores each task as it
finishes and keeps `results.json` (repo root, gitignored) in the platform's
format the whole time, so a crash or timeout still leaves a valid file. A
task passes only if `score.py` says `pass`; `score` is the fraction of its
checks that passed. Each agent run is killed after `--task-timeout` (180s)
and no task starts once `--budget-minutes` (20, under the toml's 25) is
nearly spent. The run is a dry run; add `--write` to the toml's `run` to test
writes (the instance copy is thrown away). One run per team every 3 days, so
rehearse locally with only those variables set:

```bash
AGENTSWITCH_BASE_URL=... AGENTSWITCH_TOKEN=... AGENTSWITCH_INSTANCE=suryodaya \
OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama OPENAI_MODEL=gemma4:e4b \
python agent/run_hosted.py --tasks at_risk,quote_no_bom
```

## Repo layout

```
agent/
  run.py            entry point
  run_hosted.py     hosted harness run launcher (see agentswitch-harness.toml)
  config.py         .env loading, runs/ location
  transport/        mcp_client (typed errors, re-login), llm_client (glc_v5 or OpenAI-compatible)
  domain/           deals, items, escalation, records: no LLM, pure where possible
  graph/            engine, registry (the allowlist), plans, planner, finding
  llm/              intent (classify), narrate, verify, chat_loop, tools (its curated menu)
  harness/          run_record (TaskRun), persist (platform writes), call_log,
                    tasks.json, verifiers, runner, score, hosted (results.json)
docs/               design notes, brief, recon, bug register
tests/              hand-written tests go here
```

## Testing

```bash
uv run pytest
```

pytest is configured in `pyproject.toml` (`testpaths = tests`, `agent/` on the
import path, so `from domain import deals` works). The graded tests are written
by hand, not by AI. The parts easiest to test without the network:

- `domain.deals.closing_this_month(snapshot, today)` / `at_risk(...)`: plain
  functions over a snapshot dict and a date.
- `domain.deals.snapshot_deals(client)`, `items.get_item`, `records.get_deal`,
  `escalation.file_escalation`: need only an object with `.call(name, args)`
  that returns pages or raises `MCPToolError(code=...)`.
- `graph.engine.Engine` with `plans.pipeline_review` and `RulePlanner`: a full
  graph run in memory with a fake client and `RunContext(today=...)`.
- `graph.finding.build_finding`, `llm.narrate.render_template`,
  `llm.verify.verify_claims`, `llm.intent.validate`: plain functions.
- `domain.items.resolve_item`: a fake client whose `Item.list` returns 0, 1 or
  several rows.

For assertions that hold across runs, pin `--today` and assert against the deal
snapshot saved in that run's `graph.json`, not fixed ids: the live book changes.

## Status

- **Phase 1 (done):** typed errors, one deal snapshot, the task graph with
  fixed plans and the rule planner, the finding built in code, run records,
  `--dry-run`, `--today`.
  - Checked against live data in dry runs only. **The path that writes to the
    platform hasn't been run yet.**
- **Phase 2 (done):** free-text questions classified and routed, items
  looked up by name or code, the prose checked against the finding (one
  rewrite, then the template), out-of-scope parts refused, an incomplete
  snapshot re-read once. Checked in live dry runs and offline.
- **Harness (done):** task set, platform-reading verifiers, runner and
  offline scorer (see *Harness*). Batch `20260928T040529` passes 13/13 tasks
  (129/129 checks) against the live book, in a dry run.
- **Phase 3:** pause a run until an escalation is answered, then resume it
  (AgentTask plus `--resume`); an LLM planner limited to the registry; a
  richer at-risk rule.

## Docs

| File | What |
|---|---|
| [`docs/agent_build_handoff.md`](docs/agent_build_handoff.md) | The brief: requirements and definition of done |
| [`docs/SALES_AGENT_DESIGN.md`](docs/SALES_AGENT_DESIGN.md) | Decisions, platform facts, finding schema, architecture |
| [`docs/TO_REVISIT.md`](docs/TO_REVISIT.md) | Open decisions |
| [`docs/UNHAPPY_PATHS.md`](docs/UNHAPPY_PATHS.md) | Recon: error envelope and codes, guard behaviour |
| [`docs/SALES_AGENT_PATH.md`](docs/SALES_AGENT_PATH.md) | Recon: the quote-to-cash path and its traps |
| [`docs/BUGS_FILED.md`](docs/BUGS_FILED.md) | Team 6 bug register |
