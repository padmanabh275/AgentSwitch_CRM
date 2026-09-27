# Recon findings — the quote-to-cash path as it actually behaves

*Imported from the capstone working folder. Raw responses (`out/suryodaya/...`) and the
harness scripts referenced below live there, not in this repo.*

*Observed behaviour only. No implementation — specs for Sagar to build from.*

Walked end to end on Suryodaya as `team06` (Pipeline/crm seat) on 2026-09-22.
All 12 steps green. Raw responses in `out/suryodaya/chainwalk/`, ids in `_walk.json`.
Reproduce with `python3 harness/walk_chain.py`.

Records produced by the reference walk:
`Lead(Mahesh More) -> Deal T6-chainwalk-deal -> QTN-2026-00060 -> SO-2026-00236`,
net 1000 / tax 180 / **grand 1180 carried intact all the way through**.

## The path

| # | Call | Arguments | Result |
|---|------|-----------|--------|
| 1 | `Lead.create` | `party_id`*, `company_id`, `value`, `source`, `notes` | status `new` |
| 2 | `Lead.contact` | `id` | `contacted` |
| 3 | `Lead.qualify` | `id` | `qualified` |
| 4 | `Lead.make.Deal` | `id`, `title`, `products[]` | Deal, stage `new` |
| 5 | `Deal.qualify` | `id` | `qualification` |
| 6 | `Deal.send_proposal` | `id` | `proposal` |
| 7 | `Deal.negotiate` | `id` | `negotiation` |
| 8 | `Deal.make.Quotation` | `id`, `gst_treatment`, `place_of_supply`, **`taxes[]`** | Quotation, status `draft` |
| 9 | `Quotation.send` | `id` | `sent` |
| 10 | `Quotation.accept.sent.accepted` | `id` | `accepted` |
| 11 | `Quotation.make.SalesOrder` | `id` | SalesOrder `draft`; quotation -> `converted` |
| 12 | `SalesOrder.confirm` | `id` | `confirmed` |

Every transition tool takes **`id` and nothing else**. All the data goes in at
steps 1, 4 and 8.

## Seven traps, all verified

**1. Two different response envelopes.** `create` / `update` / transitions return the
record directly. The document-chain tools (`Lead.make.Deal`, `Deal.make.Quotation`,
`Quotation.make.SalesOrder`) wrap it:

```json
{"status": "ok", "result": { ...the record... }}
```

Unwrap before reading `id`, or the chain is lost at step 4. Detection rule observed: when
`structuredContent` has exactly the keys `status` and `result`, the record is one level
down under `result`; otherwise it is the object itself.

**2. Put tax in record-level `taxes[]`. Never per line.** Filed as bug `a6899ff7`.
`Quotation.create.items` accepts 25 per-line tax fields (`cgst_amount`, `sgst_amount`,
`igst_*`, `tds_*`); they store, they read back, and they contribute **nothing** to
`total_tax` or `grand_total`. A quote built that way under-bills by the whole GST amount
with no error. Correct form:

```json
"taxes": [{"tax_type": "IGST @ 18%", "rate": 18.0, "amount": 180.0}]
```

Verified: this flows Deal -> Quotation -> SalesOrder intact (1000 / 180 / 1180 at every hop).

**3. `_quotation_id_display` is wrong. Use `quotation_id`.** Filed as bug `a55aac01`.
On SO-2026-00236 it returned `'Mahesh More'` (the party) instead of `'QTN-2026-00060'`.
Resolve the UUID yourself; never read the display field for provenance.

**4. Transition names are not uniform across entities.** Lead uses bare verbs
(`Lead.qualify`, `Lead.contact`); Deal and Quotation use `verb.<from>.<to>`
(`Deal.mark_won.negotiation.closed_won`, `Quotation.accept.sent.accepted`). There is no
rule to infer them - **read live `tools/list` and match on prefix.** The saved 247-tool
dump is stale; live is 238 and `Lead.convert.*` / `Deal.mark_won.new.*` are gone.

**5. `Lead.make.Deal` derives `value` from `products`.** We passed `value: 50000` on the
lead and `products` summing to 1000; the deal came out at **1000**. If you need the lead's
value preserved, pass `value` explicitly at step 4.

**6. Do not hand-copy `products` into `items`.** Deal uses `products[]`, Quotation and
SalesOrder use `items[]` - but `Deal.make.Quotation` **maps them for you**. Copying by
hand is what produces `products is not an accepted argument`. Use the `make` tool and the
rename is a non-issue.

**7. Winning the deal is a separate call.** `Deal.make.Quotation` leaves the deal at
`negotiation`. Nothing auto-advances it. Call `Deal.mark_won.negotiation.closed_won`
yourself once the order is confirmed.

## Where the path ends

`SalesOrder.confirm` is the last step available to this seat. The confirmed order reports
`invoiced_status: "unbilled"`, and `Invoice.*`, `Payment.*` and `SalesOrder.make.Invoice`
all return `tool_not_available`. Quote-to-cash stops at the order - plan the agent's goal
to terminate there.

## Bypasses to avoid

Live but not part of the intended path:
- `Quotation.convert_to_order.sent.converted` / `.viewed.converted` - turn an unaccepted
  quote into an order. Use `.accepted.converted` or `make.SalesOrder`.
- `Deal.mark_lost.new.closed_lost` - skips the whole funnel. (`mark_won.new` was removed;
  `mark_lost.new` was not.)
