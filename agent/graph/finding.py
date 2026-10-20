"""build_finding: the gradable object, assembled in code from node outcomes.

It never depends on what the LLM chose to call — every requested section
is present in every run, with an explicit outcome. A section whose nodes
errored or were skipped says so (with the reason) rather than going
missing; a section nobody asked for is "not_requested".
"""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from domain.deals import IST
from graph.outcome import ERROR, SKIPPED, NodeOutcome

if TYPE_CHECKING:
    from graph.registry import RunContext

SECTIONS = ("closing_this_month", "at_risk", "quote")


def _section(candidates: list[str], outcomes: dict[str, NodeOutcome]) -> dict:
    """The first candidate node (in priority order) that actually ran.

    For the quote that's escalate_quote > price_lookup > get_item >
    quote_refused: the furthest the branch got is the answer.
    """
    skipped = None
    for node_id in candidates:
        out = outcomes.get(node_id)
        if out is None:
            continue
        if out.status == SKIPPED:
            skipped = skipped or (node_id, out)
            continue
        if out.status == ERROR:
            return {"outcome": "error", "node": node_id, "error_code": out.error_code,
                    "reason": out.reason}
        return out.data
    if skipped:
        return {"outcome": "skipped", "node": skipped[0], "reason": skipped[1].reason}
    return {"outcome": "error", "reason": "no node for this section produced an outcome"}


def build_finding(args: dict, outcomes: dict[str, NodeOutcome], ctx: "RunContext") -> dict:
    asks = args["asks"]
    finding: dict = {}
    for name in SECTIONS:
        if name not in asks:
            finding[name] = {"outcome": "not_requested"}
        else:
            finding[name] = _section(args["sections"][name], outcomes)
    # Out-of-scope parts refused at planning time, and an in-scope part the
    # plan doesn't cover. Always present, so the shape doesn't vary.
    # The quote as a record, when the request named a deal (run.py --deal).
    if args.get("quotation_requested"):
        q = _section(args["sections"]["quotation"], outcomes)
        if q.get("outcome") == "answered":   # only get_deal ran: nothing to write
            q = {"outcome": "skipped", "deal_id": q.get("deal_id"),
                 "reason": f"no quotation drafted: the quote was {finding['quote'].get('outcome')}"}
        finding["quotation"] = q
    else:
        finding["quotation"] = {"outcome": "not_requested"}
    # The deal-stage move, when one was asked for. Always present.
    finding["advance_deal"] = (_section(args["sections"]["advance_deal"], outcomes)
                               if args.get("advance_requested") else {"outcome": "not_requested"})
    finding["advance_lead"] = (_section(args["sections"]["advance_lead"], outcomes)
                               if args.get("advance_lead_requested") else {"outcome": "not_requested"})
    finding["refusals"] = [outcomes[i].data for i in args.get("refusals", []) if i in outcomes]
    finding["not_handled"] = args.get("not_handled")
    finding.update({
        "run_id": ctx.run_id,
        "session_id": ctx.session_id,
        "dry_run": ctx.dry_run,
        "generated_at": dt.datetime.now(IST).isoformat(),  # local clock; no server clock source found
    })
    return finding
