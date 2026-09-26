"""narrate(): the finding -> an answer a person can read.

The finding is already complete and computed in code; this step only
words it. If the LLM is unreachable or its answer was cut off, the
deterministic template is used instead — the run still ends with an
answer, and `source` says which one it is.
"""
from __future__ import annotations

import json

from graph.outcome import ANSWERED, NodeOutcome
from transport.llm_client import LLMError, call_llm

SYSTEM_PROMPT = """You write the answer for the Pipeline/Sales agent of \
Suryodaya Precision Works (Team 6, seat 6). You are given a FINDING — JSON \
computed from live CRM data. Answer the user's question from it and nothing else.

Rules:
- Every deal, amount and date you mention must appear in the FINDING. Refer \
to deals by title and value (INR). Never compute new totals — use the ones given.
- Cover each section in order: closing_this_month, at_risk, quote. Say plainly \
whether it was answered, escalated, refused, not requested, or failed, and why.
- If pagination_complete is false, say the list may be incomplete.
- For at_risk, give the rule used and each deal's reason. Mention how many open \
deals have no close date (open_without_close_date_ids) — they can't be judged.
- For an escalated quote, say no price was invented, give the escalation number \
if there is one, and say whether an existing escalation was reused. If dry_run \
is true, say nothing was actually filed.
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
        out.append(f"Quote: {q['requested_qty']} x item {q['item_id']} at {_fmt_inr(q['unit_price'])} = {_fmt_inr(q['total_price'])} ({q['reason']}).")
    elif o == "escalated":
        if q.get("dry_run"):
            out.append(f"Quote: can't price it ({q.get('reason')}). Dry run — an escalation would be filed; nothing was filed. No price invented.")
        else:
            reused = " (existing open escalation reused)" if q.get("reused_existing") else ""
            out.append(f"Quote: can't price it ({q.get('reason')}). Escalated as {q.get('number') or q.get('escalation_id')}{reused}. No price invented.")
    elif o != "not_requested":
        out += _not_answered("Quote", q)

    return "\n".join(out)


def narrate(query: str, finding: dict) -> NodeOutcome:
    template = render_template(finding)
    user = f"Question: {query}\n\nFINDING:\n{json.dumps(finding, indent=1, default=str)}"
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]
    try:
        resp = call_llm(messages, max_tokens=1500)
    except LLMError as e:
        return NodeOutcome(ANSWERED, data={"text": template, "source": "template",
                                           "fallback_reason": str(e)})
    text = (resp.get("text") or "").strip()
    stop = resp.get("stop_reason")
    if not text or stop == "max_tokens":
        return NodeOutcome(ANSWERED, data={"text": template, "source": "template",
                                           "fallback_reason": f"llm answer unusable (stop_reason={stop!r}, {len(text)} chars)"})
    return NodeOutcome(ANSWERED, data={"text": text, "source": "llm", "stop_reason": stop,
                                       "template": template})
