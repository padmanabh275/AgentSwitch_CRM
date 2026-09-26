# Recon findings — failure modes the sales agent must branch on

*Imported from the capstone working folder. Raw responses (`out/suryodaya/...`) and the
harness scripts referenced below live there, not in this repo.*

*Observed behaviour only. 26 probes against the live seat, 2026-09-22.*
*Raw responses in `out/suryodaya/unhappy/`, matrix in `_matrix.json`. Rerun: `harness/unhappy_paths.py`.*

## The headline: errors here are self-correcting

Two things make this seat unusually agent-friendly, and both change how the control loop
should be designed:

1. **Transition errors name the legal moves.** The agent does not need a hardcoded state map.
2. **Argument errors name the field and the fix.** One retry is usually enough.

Both contradict what the gap report says (P0-4, "errors do not name the missing field"). That
finding is stale — see *Corrections* at the end.

## Error envelope

Every failure below returns **HTTP 200** with a JSON-RPC error inside. Nothing in this matrix
returned a non-200. Branch on `error.data.code`, never on HTTP status.

```
{"error": {"code": -32602,
           "message": "<human readable, often actionable>",
           "data": {"code": "<branch on this>", "errors": [{"field": "...", "message": "..."}]}}}
```

## The four codes, and what the agent should do with each

| `data.code` | Means | Agent action |
|---|---|---|
| `invalid_transition` | Right tool, wrong current state | **Re-read the record, then follow the legal moves the message names.** Recoverable. |
| `invalid_arguments` | Schema violation | **Fix the named field and retry once.** Recoverable. |
| `not_found` | No such record | Do not retry. Re-resolve the id, or escalate. |
| `tool_not_available` | Tool absent from this seat | Do not retry. Structural — the capability does not exist. |

### `invalid_transition` — the message is a map

Calling `Deal.negotiate` on a deal in `new`:

> No transition from 'new' to 'negotiation' in flow 'DealFlow': this tool takes the edge from
> 'proposal' (action 'Negotiate'). **From 'new' you can go to: closed_lost (Mark Lost);
> qualification (Qualify)**

It names the current state, the edge this tool wanted, and every legal move from where the record
actually is. An agent that hits this can recover without any prior knowledge of the state machine.
**Design the agent to parse this rather than to carry a hardcoded stage graph** — the graph drifts
(`Deal.mark_won.new` and `Lead.convert.*` were removed between 16 and 22 Sep), the error does not.

### `invalid_arguments` — the message is a fix

| What we sent | `field` | `message` |
|---|---|---|
| `{}` on `Deal.get` | `/id` | `id is required.` |
| unknown key | `/nope` | `nope is not an accepted argument of this tool.` |
| bad enum | `/source` | `/source must be one of: website, referral, campaign, cold_call, social_media, partner, existing_customer.` |
| `id: 12345` | `/id` | `/id must be string.` |
| `party_id` that does not exist | — | `Referenced Party does not exist` |
| `title: ""` | — | `Title is required` |

The enum case returns **the entire allowed set**, so a bad enum costs one retry, not a schema lookup.

## Guard behaviour — what is and is not enforced

**Transitions are properly guarded.** Every illegal move was rejected:

| Probe | Result |
|---|---|
| `Deal.negotiate` on `new` | rejected |
| `Deal.mark_won.negotiation.closed_won` on `new` | rejected |
| `Deal.send_proposal` on `new` | rejected |
| `Deal.qualify` on `new` | **allowed** (legal edge) |
| `Deal.qualify` again, now `qualification` | rejected |
| any transition on a `closed_lost` deal | rejected |

**Terminal records lock on confirmation, not on creation.** A `closed_lost` deal still accepts
`Deal.update` for ordinary fields (we set `value: 999` on one). A `confirmed` SalesOrder rejects
everything: *"Cannot modify SalesOrder in 'confirmed' status. Only notes/tags/assignments can be
changed."* Same for a `converted` Quotation.

**State fields are schema-visible but runtime-rejected.** `Deal.update` lists `stage` among its
properties; sending it returns *"State fields ['stage'] cannot be changed via PUT. Use POST
/api/Deal/{id}/transition instead."* Same shape for `Lead.status`. The agent must know that
`stage`/`status` appearing in an `update` schema does not mean they are writable — use the
transition tools. (Note the message advises a REST path an MCP client cannot call; the MCP
equivalent is the transition tool.)

## Gotchas worth designing around

**1. `tool_not_available` is overloaded.** A tool that is out-of-app (`Invoice.list`), one that does
not exist at all (`Totally.Fake.Tool`), and one this seat may not use (`Deal.delete`) all return the
identical code and message. The agent cannot distinguish "typo", "not permitted" and "does not
exist" — treat all three as structural and do not retry.

**2. A malformed id returns `not_found`, not `invalid_arguments`.** `Deal.get({id:"not-a-uuid"})`
answers *"Deal not found."* So the agent cannot tell a corrupted id from a deleted record. Validate
id shape before calling if that distinction matters.

**3. `invoiced_status` is directly writable while a SalesOrder is `draft`.** We set
`invoiced_status: "invoiced"` on SO-2026-00237 with `billed_qty: 0` and no Invoice entity existing
anywhere in the seat. On confirmation it locks with that value. The agent should never write this
field — it is derived state with nothing backing it, and a wrong value becomes permanent.

**4. Everything is HTTP 200.** Any non-200 is a transport or auth problem, not a tool failure.

## Suggested agent control flow

```
call tool
├── no error                    -> proceed
└── error.data.code
    ├── invalid_arguments       -> fix the named field, retry ONCE, else escalate
    ├── invalid_transition      -> re-read record; if a legal move from the message reaches
    │                              the goal, take it; else escalate (do not force)
    ├── not_found               -> re-resolve the id once, else escalate
    └── tool_not_available      -> structural; abandon this approach, escalate
```

Escalation surface exists and is writable from this seat: `AgentEscalation.create/update/list/get`.

## Corrections to the existing register

- **Gap report P0-4** ("errors do not name the missing field, `path: "/"`") — **fixed.** Errors now
  return `field` and an actionable `message`.
- **Filed bug `8ae6f7c8`** ("Deal state-machine transitions ignore their encoded source-stage
  guard") — **not reproducible today.** All six illegal-transition probes were correctly rejected.
  Either it has been fixed since 17 Sep or the original repro differed. Worth re-checking before it
  is cited to the app owner.
