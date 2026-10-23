"""Plan templates: a request's shape -> the initial graph.

pipeline_review is the seat's composite question ("what closes this month,
what is at risk, and quote N units"). Only the asked-for branches are
built; the planner (graph/planner.py) extends the graph from there.

    snapshot_deals (+ reread_deals) ─┬─ closing_this_month ─┐
                                     └─ at_risk ────────────┤
    resolve_item ── price_lookup ──(+ escalate_quote)───────┤
    get_deal ──────────────(+ create_quotation ── send_quotation)┤  (with a deal, priced)
             └─────────(+ draft_for_approval ── submit_for_approval)┤  (with a deal, unpriced)
    advance_deal (move a deal one stage) ───────────────────┤
    advance_lead (move a lead one status) ──────────────────┤
    accept_quote (accepted quotation -> sales order) ───────┤
    refuse_1..n (out-of-scope parts) ───────────────────────┤
                                            build_finding (needs=done) ── narrate
"""
from __future__ import annotations

from domain.deal_stage import ADVANCE_STAGES  # noqa: F401  (run.py's --to-stage choices)
from domain.lead_stage import ADVANCE_STATUSES  # noqa: F401  (run.py's --to-status choices)
from graph.engine import TaskSpec

ASKS = ("closing_this_month", "at_risk", "quote")

# Where each list reads its deals from: a planner-added re-read, if one
# answered, else the original snapshot.
SNAPSHOT_SOURCES = ["reread_deals", "snapshot_deals"]

# Priority order for each finding section: the furthest node that ran wins.
# snapshot_deals comes after the filters so that when the read itself fails,
# the section reports that error (with its code), not just "skipped".
SECTION_SOURCES = {
    "closing_this_month": ["closing_this_month", "snapshot_deals"],
    "at_risk": ["at_risk", "snapshot_deals"],
    "quote": ["escalate_quote", "price_lookup", "resolve_item", "quote_refused"],
    # get_deal last: it only answers this section when it refused (no such
    # deal, or a closed one); otherwise the write nodes do.
    "quotation": ["submit_for_approval", "draft_for_approval",
                  "send_quotation", "create_quotation", "get_deal"],
    "advance_deal": ["advance_deal"],
    "advance_lead": ["advance_lead"],
    "accept_quote": ["accept_quote"],
}

NOT_HANDLED_REASON = ("outside this plan's three questions — ask it on its own "
                      "(run.py --chat) so the lookup tools can answer it")


def pipeline_review(asks: list[str], item_ref: str | None = None, qty: int | None = None,
                    out_of_scope: list[dict] | None = None,
                    other: str | None = None, deal_id: str | None = None,
                    quote_deal_ref: str | None = None,
                    advance: dict | None = None, advance_lead: dict | None = None,
                    accept_quote: dict | None = None) -> list[TaskSpec]:
    """item_ref may be an id, code or name. qty None means "not stated":
    the quote is refused rather than assuming a quantity. deal_id makes the
    quote a Quotation on that deal (the planner adds the writes once the
    price is known); without it the quote is only a number in the finding.
    quote_deal_ref is the same deal as a request names it (id or exact
    title); it is used when deal_id isn't given.
    advance ({"deal_ref", "target_stage"}) moves that deal one stage forward;
    whether the step is allowed is decided in domain/deal_stage.py.
    advance_lead ({"lead_ref", "target_status"}) does the same for a lead
    (domain/lead_stage.py). accept_quote ({"quotation_ref",
    "customer_accepted"}) turns that quotation into a sales order
    (domain/quote_order.py)."""
    unknown = set(asks) - set(ASKS)
    if unknown:
        raise ValueError(f"unknown asks {sorted(unknown)}; expected a subset of {ASKS}")

    tasks: list[TaskSpec] = []
    leaves: list[str] = []

    if "closing_this_month" in asks or "at_risk" in asks:
        tasks.append(TaskSpec("snapshot_deals", "snapshot_deals"))
        for name in ("closing_this_month", "at_risk"):
            if name in asks:
                tasks.append(TaskSpec(name, name, args={"snapshot": SNAPSHOT_SOURCES},
                                      deps=["snapshot_deals"]))
                leaves.append(name)

    if "quote" in asks:
        missing = [what for what, val in (("which item", item_ref), ("how many units", qty)) if not val]
        if missing:
            tasks.append(TaskSpec("quote_refused", "refuse", args={
                "requested_qty": qty, "item_id": None, "query": item_ref,
                "reason": f"can't quote without knowing {' and '.join(missing)}",
            }))
            leaves.append("quote_refused")
        else:
            tasks.append(TaskSpec("resolve_item", "resolve_item", args={"ref": item_ref}))
            tasks.append(TaskSpec("price_lookup", "price_lookup",
                                  args={"item": "resolve_item", "qty": qty}, deps=["resolve_item"]))
            leaves += ["resolve_item", "price_lookup"]
            if deal_id or quote_deal_ref:
                tasks.append(TaskSpec("get_deal", "get_deal", args=(
                    {"deal_id": deal_id} if deal_id else {"deal_ref": quote_deal_ref})))
                leaves.append("get_deal")

    if advance:
        tasks.append(TaskSpec("advance_deal", "advance_deal", args={
            "deal_ref": advance.get("deal_ref"), "target_stage": advance.get("target_stage")}))
        leaves.append("advance_deal")
    if advance_lead:
        tasks.append(TaskSpec("advance_lead", "advance_lead", args={
            "lead_ref": advance_lead.get("lead_ref"),
            "target_status": advance_lead.get("target_status")}))
        leaves.append("advance_lead")
    if accept_quote:
        tasks.append(TaskSpec("accept_quote", "accept_quote", args={
            "quotation_ref": accept_quote.get("quotation_ref"),
            "customer_accepted": bool(accept_quote.get("customer_accepted"))}))
        leaves.append("accept_quote")

    refusals = []
    for i, part in enumerate(out_of_scope or [], start=1):
        node_id = f"refuse_{i}"
        tasks.append(TaskSpec(node_id, "refuse", args={"request": part["request"],
                                                       "reason": part["reason"]}))
        refusals.append(node_id)
    leaves += refusals

    not_handled = {"request": other, "reason": NOT_HANDLED_REASON} if other else None
    tasks.append(TaskSpec("build_finding", "build_finding", needs="done", deps=leaves,
                          args={"asks": list(asks), "sections": SECTION_SOURCES,
                                "refusals": refusals, "not_handled": not_handled,
                                "quotation_requested": bool((deal_id or quote_deal_ref) and "quote" in asks),
                                "advance_requested": bool(advance),
                                "advance_lead_requested": bool(advance_lead),
                                "accept_quote_requested": bool(accept_quote)}))
    tasks.append(TaskSpec("narrate", "narrate", args={"finding": "build_finding"},
                          deps=["build_finding"]))
    return tasks
