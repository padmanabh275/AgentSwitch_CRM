"""Plan templates: a request's shape -> the initial graph.

pipeline_review is the seat's composite question ("what closes this month,
what is at risk, and quote N units"). Only the asked-for branches are
built; the planner (graph/planner.py) extends the graph from there.

    snapshot_deals (+ reread_deals) ─┬─ closing_this_month ─┐
                                     └─ at_risk ────────────┤
    resolve_item ── price_lookup ──(+ escalate_quote)───────┤
    refuse_1..n (out-of-scope parts) ───────────────────────┤
                                            build_finding (needs=done) ── narrate
"""
from __future__ import annotations

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
}

NOT_HANDLED_REASON = ("outside this plan's three questions — ask it on its own "
                      "(run.py --chat) so the lookup tools can answer it")


def pipeline_review(asks: list[str], item_ref: str | None = None, qty: int | None = None,
                    out_of_scope: list[dict] | None = None,
                    other: str | None = None) -> list[TaskSpec]:
    """item_ref may be an id, code or name. qty None means "not stated":
    the quote is refused rather than assuming a quantity."""
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
                                "refusals": refusals, "not_handled": not_handled}))
    tasks.append(TaskSpec("narrate", "narrate", args={"finding": "build_finding"},
                          deps=["build_finding"]))
    return tasks
