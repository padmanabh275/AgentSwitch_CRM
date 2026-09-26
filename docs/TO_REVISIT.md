# To revisit later

Open questions and decisions parked mid-conversation — not blocking, but not
settled either. Each entry: what's undecided, why it came up, and where the
relevant code/docs live.

---

## 1. Is `AgentMemory.create` worth keeping as the finding-storage step?

**Raised:** 2026-09-26, during the first live end-to-end run of `agent.py`.

**Context:** The three sub-questions (closing this month / at risk / quote)
are already fully answered by `list_closing_this_month`/`list_at_risk`/
`attempt_quote` themselves. `AgentMemory.create` is an *extra* step whose
only job is to persist the structured finding JSON somewhere durable and
machine-readable, so a grader doesn't have to re-parse the agent's prose
answer. It's a workaround, not a requirement: `/api/agent/evidence/*` (the
natural home for a structured answer) 403s for this seat — belongs to the
accounting app — and there's no other purpose-built "submit your finding"
tool anywhere in the 242-tool catalog. `category: "context"` is a repurposed
fit, not its intended use (durable facts/preferences about a party).

**Question:** keep it, or drop the persistence step entirely and let the
printed finding JSON in the agent's own stdout/logs be the record?

**Where:** `agent.py`'s `main()` (the `AgentSession.create` + `AgentMemory.create`
calls at the end of the run); design rationale in `SALES_AGENT_DESIGN.md`
under "Platform facts".

**Status:** unresolved, not blocking — the agent works either way.
