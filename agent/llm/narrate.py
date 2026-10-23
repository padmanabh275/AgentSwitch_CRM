"""narrate(): the finding -> an answer a person can read.

The finding is already complete and computed in code; this step only
words it. Every LLM answer is checked against the finding
(llm/verify.py); a failing answer gets one rewrite with the specific
problems listed. If it still fails — or the LLM is unreachable, or the
answer was cut off — the deterministic template is used instead. The run
always ends with an answer, and `source` says which one it is.
"""
from __future__ import annotations

import json

from graph.outcome import ANSWERED, NodeOutcome
from llm.verify import feedback, verify_claims
from transport.llm_client import LLMError, call_llm

MAX_ATTEMPTS = 2   # the first answer, plus one rewrite

SYSTEM_PROMPT = """You write the answer for the Pipeline/Sales agent of \
Suryodaya Precision Works (Team 6, seat 6). You are given a FINDING — JSON \
computed from live CRM data. Answer the user's question from it and nothing else.

Rules:
- Every deal, amount and date you mention must appear in the FINDING. Refer \
to deals by title and value. Write every amount in full digits with ₹, exactly \
as in the FINDING (e.g. ₹2,64,000.00) — no lakh/crore/million, no rounding. \
Never compute new totals — use the ones given. Don't quote record ids.
- Cover each section in order: closing_this_month, at_risk, quote. Say plainly \
whether it was answered, escalated, refused, not requested, or failed, and why.
- If pagination_complete is false, say the list may be incomplete.
- For at_risk, give the rule used and each deal's reason. Mention how many open \
deals have no close date (open_without_close_date_ids) — they can't be judged.
- For a quoted quote, give the unit BOM price, quantity and total, and say \
it's before tax and discounts.
- For a refused quote, say plainly that you can't quote it ("I can't quote \
...") and why, in the words of its reason. Only then may you ask for what's \
missing (e.g. which item); never answer with just a question.
- For an escalated quote, say why there's no BOM price, in the words of its reason. If bom_price_redacted \
is true, the price exists but is hidden from this seat: never say it isn't set. Say that no price was \
invented, give the escalation number \
if there is one, and say whether an existing escalation was reused. If dry_run \
is true, say this was a dry run and nothing was actually filed.
- If quotation is present and not "not_requested": for "sent", give the \
quotation number, the deal title and that it was sent; for "refused", say \
no quotation was drafted or sent and why (e.g. the deal changed underneath \
the run); for "would_send", say it was a dry run and nothing was written; \
for "pending_approval", give the quotation number and the deal title, say it \
was drafted with no rate because the BOM price isn't visible to this seat, \
and that it is waiting for margin approval (no price was invented); for \
"would_submit", say it was a dry run and nothing was written.
- If advance_deal is present and not "not_requested": for "advanced", give the \
deal title and that it moved from stage_before to stage_after; for "refused", \
say the deal was not moved and why, in the words of its reason; for \
"would_advance", say it was a dry run and nothing was written.
- If advance_lead is present and not "not_requested": for "advanced", say the \
lead moved from status_before to status_after, and if it was converted give \
the new deal's title (deal_title); for "refused", say the lead was not moved \
and why, in the words of its reason; for "would_advance", say it was a dry \
run and nothing was written.
- If accept_quote is present and not "not_requested": for "ordered", give the \
quotation number, the sales order number (order_number) and that it was made \
from the accepted quotation, and say what the deal is now (deal_stage_after: \
the platform marks it closed_won when the order is made; you never did; if \
it is anything else, say the deal is still at that stage, and if deal_value is \
0, that the platform doesn't mark a deal won without a value); for \
"refused", say no order was made and why, in the words of its reason; for \
"would_order", say it was a dry run and nothing was written.
- For an "unpriced" quote, say why there's no BOM price, in the words of its reason (if \
bom_price_redacted is true it's hidden from this seat, not unset), and that no price was invented.
- If there are refusals, say plainly that you can't do each one ("I can't ...") and why; \
don't just point elsewhere. If not_handled is \
set, say that part wasn't answered and why.
- Be direct and concise."""


def _fmt_inr(v) -> str:
    return "n/a" if v is None else f"₹{v:,.2f}"


def _deal_lines(section: dict, with_reason: bool = False) -> list[str]:
    lines = []
    for d in section.get("deals", []):
        line = f"  - {d.get('title')} — {_fmt_inr(d.get('value'))}, stage {d.get('stage')}, close {d.get('expected_close_date')}"
        if with_reason:
            reason = section.get("reasons", {}).get(d.get("id"))
            if reason:
                line += f" ({reason})"
        lines.append(line)
    return lines


def _not_answered(title: str, s: dict) -> list[str]:
    return [f"{title}: {s.get('outcome')}" + (f" — {s['reason']}" if s.get("reason") else "")]


def render_template(finding: dict) -> str:
    """Deterministic fallback: every fact comes straight from the finding."""
    out: list[str] = []

    c = finding.get("closing_this_month", {})
    if c.get("outcome") == "answered":
        out.append(f"Closing in {c['month']}: {len(c['deal_ids'])} open deal(s), total {_fmt_inr(c.get('total_value'))}.")
        out += _deal_lines(c)
        if not c.get("pagination_complete"):
            out.append("  (deal list may be incomplete — pagination did not complete)")
    elif c.get("outcome") != "not_requested":
        out += _not_answered("Closing this month", c)

    r = finding.get("at_risk", {})
    if r.get("outcome") == "answered":
        out.append(f"At risk ({r['rule']}): {len(r['deal_ids'])} deal(s), total {_fmt_inr(r.get('total_value'))}.")
        out += _deal_lines(r, with_reason=True)
        n = len(r.get("open_without_close_date_ids", []))
        if n:
            out.append(f"  {n} open deal(s) have no expected close date and can't be judged by this rule.")
        if not r.get("pagination_complete"):
            out.append("  (deal list may be incomplete — pagination did not complete)")
    elif r.get("outcome") != "not_requested":
        out += _not_answered("At risk", r)

    q = finding.get("quote", {})
    o = q.get("outcome")
    if o == "quoted":
        out.append(f"Quote: {q['requested_qty']} x {q.get('item_name') or 'item ' + str(q['item_id'])} at {_fmt_inr(q['unit_price'])} = {_fmt_inr(q['total_price'])} ({q['reason']}).")
    elif o == "escalated":
        if q.get("dry_run"):
            out.append(f"Quote: can't price it ({q.get('reason')}). Dry run — an escalation would be filed; nothing was filed. No price invented.")
        else:
            reused = " (existing open escalation reused)" if q.get("reused_existing") else ""
            out.append(f"Quote: can't price it ({q.get('reason')}). Escalated as {q.get('number') or q.get('escalation_id')}{reused}. No price invented.")
    elif o == "unpriced":
        out.append(f"Quote: can't price it ({q.get('reason')}). No price invented.")
    elif o != "not_requested":
        out += _not_answered("Quote", q)

    qt = finding.get("quotation") or {}
    qo = qt.get("outcome")
    if qo == "sent":
        out.append(f"Quotation {qt.get('number')} drafted on deal {qt.get('deal_title')!r} for "
                   f"{qt.get('requested_qty')} units at {_fmt_inr(qt.get('unit_price'))} = "
                   f"{_fmt_inr(qt.get('expected_net_total'))} before tax, and sent (read back as sent).")
    elif qo == "would_send":
        out.append(f"Quotation: dry run — a quotation on deal {qt.get('deal_title')!r} would be "
                   "drafted and sent; nothing was written.")
    elif qo == "pending_approval":
        out.append(f"Quotation {qt.get('number')} drafted on deal {qt.get('deal_title')!r} for "
                   f"{qt.get('requested_qty')} units with no rate (the BOM price isn't visible "
                   "to this seat), and submitted for margin approval (read back as "
                   "pending_approval). No price invented.")
    elif qo == "would_submit":
        out.append(f"Quotation: dry run — an unpriced quotation on deal {qt.get('deal_title')!r} "
                   "would be drafted and submitted for margin approval; nothing was written.")
    elif qo == "refused":
        out.append(f"Quotation: refused — {qt.get('reason')}. No quotation was sent.")
    elif qo and qo != "not_requested":
        out += _not_answered("Quotation", qt)

    a = finding.get("advance_deal") or {}
    ao = a.get("outcome")
    if ao == "advanced":
        out.append(f"Deal {a.get('deal_title')!r} moved from {a.get('stage_before')} to "
                   f"{a.get('stage_after')} (read back as {a.get('stage_after')}).")
    elif ao == "would_advance":
        out.append(f"Deal stage: dry run — deal {a.get('deal_title')!r} would move from "
                   f"{a.get('stage_before')} to {a.get('next_stage')}; nothing was written.")
    elif ao == "refused":
        out.append(f"Deal stage: I can't move it — {a.get('reason')}.")
    elif ao and ao != "not_requested":
        out += _not_answered("Deal stage", a)

    ld = finding.get("advance_lead") or {}
    lo = ld.get("outcome")
    if lo == "advanced":
        made = f", which created deal {ld.get('deal_title')!r}" if ld.get("deal_title") else ""
        out.append(f"Lead moved from {ld.get('status_before')} to {ld.get('status_after')}"
                   f"{made} (read back as {ld.get('status_after')}).")
    elif lo == "would_advance":
        out.append(f"Lead: dry run — it would move from {ld.get('status_before')} to "
                   f"{ld.get('next_status')}; nothing was written.")
    elif lo == "refused":
        out.append(f"Lead: I can't move it — {ld.get('reason')}.")
    elif lo and lo != "not_requested":
        out += _not_answered("Lead", ld)

    aq = finding.get("accept_quote") or {}
    ao2 = aq.get("outcome")
    if ao2 == "ordered":
        out.append(f"Sales order {aq.get('order_number')} made from quotation {aq.get('number')} "
                   f"({aq.get('reason')}).")
    elif ao2 == "would_order":
        out.append(f"Sales order: dry run — {aq.get('reason')}.")
    elif ao2 == "refused":
        out.append(f"Sales order: I can't make it — {aq.get('reason')}.")
    elif ao2 and ao2 != "not_requested":
        out += _not_answered("Sales order", aq)

    for ref in finding.get("refusals") or []:
        out.append(f"Refused: {ref.get('request')} — {ref.get('reason')}")
    nh = finding.get("not_handled")
    if nh:
        out.append(f"Not answered: {nh.get('request')} — {nh.get('reason')}")

    return "\n".join(out)


def narrate(query: str, finding: dict) -> NodeOutcome:
    template = render_template(finding)
    # The facts verify_claims will require, stated up front: without this the
    # first answer usually omits one and costs a rewrite.
    required = verify_claims("", finding)["missing"]
    must = ("\n\nYour answer must state: " + "; ".join(required) + ".") if required else ""
    user = f"Question: {query}\n\nFINDING:\n{json.dumps(finding, indent=1, default=str)}{must}"
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]
    attempts: list[dict] = []

    def fallback(reason: str) -> NodeOutcome:
        return NodeOutcome(ANSWERED, data={"text": template, "source": "template",
                                           "fallback_reason": reason, "attempts": attempts})

    for _ in range(MAX_ATTEMPTS):
        try:
            resp = call_llm(messages, max_tokens=1500)
        except LLMError as e:
            return fallback(str(e))
        text = (resp.get("text") or "").strip()
        stop = resp.get("stop_reason")
        if not text or stop == "max_tokens":
            return fallback(f"llm answer unusable (stop_reason={stop!r}, {len(text)} chars)")
        check = verify_claims(text, finding)
        attempts.append({"text": text, "verification": check})
        if check["ok"]:
            return NodeOutcome(ANSWERED, data={"text": text, "source": "llm",
                                               "verification": check, "attempts": attempts,
                                               "template": template})
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": feedback(check)}]
    return fallback(f"llm answer failed verification {MAX_ATTEMPTS} times")
