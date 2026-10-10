# Harness evidence

Scored batches from `agent/harness`, newest first, run 2026-10-05 against live Suryodaya, all as dry runs
(reads only), today pinned to 2026-09-28.

| Batch | Tasks | Result |
|---|---|---|
| **`20261005T234110`** (latest) | all 14 | **14 pass.** Adds `refuse_not_permitted_mark_lost` and the chat-path answer check. That task's answer is the template: the LLM's answer and its one rewrite both claimed a dry-run escalation "has been filed", which the check rejected (recorded in the run's `warnings`). |
| `20261005T220916` | all 13 | 12 pass, 1 fail: `week1_refuse_merge` ended in error because Gemini returned 503 "high demand" during intent classification. Not an agent or scorer fault; left as recorded. |
| `20261005T221626` | `week1_refuse_merge` only | pass, re-run after the 503 |

Each run directory holds `task.json` (the task as run), `taskrun.json` (the agent's finding
and answer) and `score.json` (every check). The raw platform reads (`graph.json`,
`ground_truth_before/after.json`, `calls.jsonl`) are left out because this repo is public and
they hold full copies of the shared deal book. They stay in the local `runs/`.

Re-score from the full local copies with `cd agent && uv run python -m harness.score ../runs/batches/<batch>`.
