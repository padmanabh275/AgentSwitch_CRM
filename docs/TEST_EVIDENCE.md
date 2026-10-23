# Test evidence: do our checks catch a broken agent?

A test or a verifier is only worth something if it fails when the thing it
guards is broken. We checked both layers the way Team 20 does: break
something on purpose, then confirm the check notices.

Run on 2026-10-23. Both scripts are offline: no platform, no LLM, and they
change nothing in the working tree or in `runs/`.

## 1. Unit tests: break the code, a test must fail

```bash
uv run python scripts/mutation_probe.py
```

The probe copies the repo to a temporary folder, plants one bug at a time,
and runs `pytest` (both `tests/` and `tests_ai/`). Result: **17 of 17 planted
bugs caught.**

| Planted bug | Code | Caught by |
|---|---|---|
| at-risk counts deals due today (`<=`) | `domain/deals.py` | `tests/test_deals.py`, `tests/test_verifiers.py` |
| closing this month ignores the year | `domain/deals.py` | `tests/test_deals.py` |
| "today" in UTC, not IST | `domain/deals.py` | `tests_ai/test_mutation_gaps.py` (added) |
| `pagination_complete` always true | `domain/deals.py` | `tests/test_deals.py` |
| mixed currencies summed into one total | `domain/deals.py` | `tests/test_deals.py` |
| deals not sorted largest first | `domain/deals.py` | `tests/test_deals.py` |
| MCP error inside HTTP 200 read as success | `transport/mcp_client.py` | `tests/test_mcp_http.py` |
| MCP `isError` read as success | `transport/mcp_client.py` | `tests/test_mcp_http.py` |
| open escalation never reused | `domain/escalation.py` | `tests/test_escalation_and_records.py` |
| prose verifier always ok | `llm/verify.py` | `tests/test_verify.py` |
| quote skips the pre-write deal re-read | `domain/quotations.py` | `tests_ai/` quotation tests |
| sent quote ordered without the customer accepting | `domain/quote_order.py` | `tests_ai/test_accept_quote.py` |
| harness `calls_policy` never fails | `harness/verifiers.py` | `tests/test_harness_checks.py` |
| harness deal-set ids never fail | `harness/verifiers.py` | `tests/test_verifiers.py` |
| harness deal-set total never fails | `harness/verifiers.py` | `tests_ai/test_mutation_gaps.py` (added) |
| harness `refusal_answer` passes invented ids | `harness/verifiers.py` | `tests_ai/test_refusal_answer.py` |
| harness drift test ignores `updated_at` | `harness/verifiers.py` | `tests/test_verifiers.py` |

Before 2026-10-23 two of these survived every test ("today" in UTC, and the
deal-set total), and plain `pytest` ran only `tests/`, which let five through.
`pyproject.toml` now runs both folders.

The 17 bugs were chosen to hit the riskiest lines; it is a sample, not a
coverage figure. When you change one of those lines, the probe says
`NOT APPLIED` and needs updating.

## 2. Harness: plant a fault in a passing run, the scorer must fail it

```bash
cd agent
uv run python -m harness.calibrate ../runs/batches/<batch_id> [more batches]
```

`harness/calibrate.py` takes saved runs that pass under the current
verifiers, copies each one in memory once per fault that applies, plants the
fault and re-scores the copy. The targeted check must then say `fail`. It has
30 fault kinds: a write during a dry run, a disallowed write, a session or
escalation left behind by a dry run, a dropped deal, a wrong total, wrong days
overdue, a missing reason, a price on an unpriced quote, a changed quote
outcome, a wrong escalation subject, a refusal not said, an invented id or ₹
amount in the answer, a false escalation claim, a bypass transition, a deal or
lead moved or written when it shouldn't be, a deal or lead not advanced, an
extra step, a second deal from a lead, and more.

Run on batches `20261005T234110`, `20261023T130615`, `20261023T050857`,
`20261023T052726` and `20261023T053408` (local `runs/`, not committed: they
hold copies of the shared deal book). Result: **282 of 283 planted faults
caught.**

**The one miss is a real scorer gap.** `refusal_answer/no_false_escalation`
lets an answer say "I have escalated this" when nothing was filed, as long as
the words "dry run" appear anywhere else in the answer
(`refuse_not_permitted_mark_lost`, whose template answer says "Dry run: an
escalation would be filed"). Not fixed in this change: `verifiers.py` also
scores the hosted run, so its fix goes in on its own. Once it lands, this
fault is caught too.

**Verifiers no fault exercises yet:** `no_duplicate_quote`,
`quote_lifecycle_unchanged`, `unpriced_quote_blocked`. These score write
tasks only; the script lists them each time it runs.

## What this does not show

- Both scripts check that our checks *can* fail. They don't run the real agent
  or LLM; that is the harness's job (`harness/runner.py`, `harness/hosted.py`).
- Each harness task still runs once. We have no repeat-and-require-all-pass
  (pass^k) and no offline copy of the platform.
- The tests in `tests_ai/` are AI-written, as their headers say.
