# Handoff: Build the sales agent — AgentSwitch (Team 6)

## What we're building
A **client agent** (a loop), not a server. AgentSwitch already exposes the MCP server and REST API — the agent *calls* them. **MCP is primary**; use REST/UI only for recon and for the harness's verifiers.

The agent must answer our seat's request end to end:
> "What closes this month, what is at risk, and quote 500 units off the real BOM price."

Note: Claude Code **can** build the agent (that's the deliverable). It must **not** write the graded test suite — those tests score zero if AI-written and are Sagar's to hand-write.

## The core loop
```
read goal → re-read current state from AgentSwitch → decide next action
  → (call a tool | escalate | refuse) → observe result → repeat until done
```
Key discipline: **re-read before acting.** Data changes underneath us (Team 07 shares the book), so never act on a stale read. If a value looks off, read again before deciding.

## Functional requirements
1. **"What closes this month"** — read Opportunities; filter by stage + close date within the current month. Return the list with amounts. (Get exact field names from the domain-recon output.)
2. **"What is at risk"** — define risk explicitly and apply it consistently. Candidate signals: stale last-activity, close date already passed or slipping, stage not advancing, unusually large deal. Document the definition; the agent should be able to say *why* a deal is flagged.
3. **"Quote 500 units off the real BOM price"** — the hard part:
   - BOM lives in the manufacturing app (Team 04). Our seat can't reach it → expect 403 / no tool.
   - Correct behaviour is **escalate** (request the price via the documented escalation path) **or refuse** — clearly stating it can't obtain the BOM price. **Never invent a price.**
   - This is our "correct answer is refusal" showcase. Make the refusal explicit and reasoned, not a crash.

## Behavioural requirements (how it must act)
- **Escalation:** when it hits a permission wall or missing tool for something legitimately needed, it escalates to a human/EA rather than faking a result or giving up silently.
- **Refusal:** when there's no legitimate answer available, it refuses clearly and says why. At least one task in our set should have refusal as the correct answer.
- **No invented data:** every number it reports must trace to a real record it read. No guessing prices, amounts, or dates.
- **Respect the boundary:** don't try to sneak cross-app data through embedded fields or filters. A 403 is expected, not a puzzle to route around.
- **Don't tidy data we didn't create.** If it must write, prefix records `T6-` and never delete.
- **Handle MCP errors properly:** failures come back as **HTTP 200 with an error inside the JSON body** — check the body, not just the status, and react (retry / escalate / refuse), don't treat a 200 as success blindly.

## Uses our private agent space
The `agent` entities (AgentMemory / AgentMessage / AgentSkill) are ours to use for the agent's memory, messages, and reusable skills. Use them rather than local state where it makes sense — the platform expects the agent to live there.

## Definition of done (for this handoff)
- Agent runs the three-part request against **live** Suryodaya data and produces: the closing list, the at-risk list with reasons, and a correct quote-handling outcome (escalate or refuse).
- Runs are observable/logged so the harness can later score them.
- A short README: the risk definition used, the escalation path, and the refusal conditions.

## Out of scope here
- The harness and verifiers (separate handoff).
- The hand-written graded tests (Sagar).
- Filing bugs (candidate list → Sagar).