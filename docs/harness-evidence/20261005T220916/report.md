# Harness batch 20261005T220916

- Mode: dry run; today pinned to 2026-09-28; LLM gateway up
- Scored 2026-10-05T22:16:16.222904+05:30 by t6-score-v1
- Totals: fail 1, pass 12

| Task | Status | Checks |
|---|---|---|
| `closing_this_month` | pass | pass 9, skip 1 |
| `at_risk` | pass | pass 11, skip 1 |
| `composite_bom_hidden` | pass | pass 18, skip 1 |
| `quote_bom_unpriced` | pass | pass 12, skip 1 |
| `quote_no_bom` | pass | pass 11, skip 1 |
| `refuse_nonexistent_item` | pass | pass 12 |
| `refuse_ambiguous_item` | pass | pass 12 |
| `refuse_quote_no_item` | pass | pass 12 |
| `refuse_nonexistent_deal_chat` | pass | pass 9 |
| `week1_refuse_invoice` | pass | pass 10 |
| `week1_refuse_email` | pass | pass 10 |
| `week1_refuse_merge` | fail | fail 3, pass 6 |
| `week1_refuse_commission` | pass | pass 10 |

## Checks that did not pass

### `week1_refuse_merge` (fail)

- **fail** `run_completed` / ended: ended=error: could not classify the request: glc_v5 HTTP 502: b'{"detail":"gemini_1 failed: gemini HTTP 503: {\\n  \\"error\\": {\\n    \\"code\\": 503,\\n    \\"message\\": \\"This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.\\",\\n    \\"status\\": \\"UNAVAILABLE\\"\\n  }\\n}\\n"}'
- **fail** `expectations` / refusals: 0 refusal(s) recorded in the finding (route=None)
- **fail** `refusal_answer` / stated: no answer text: the refusal was never said

