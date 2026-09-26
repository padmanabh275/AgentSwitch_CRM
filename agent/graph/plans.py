"""Plan templates: a request's shape -> the initial graph.

pipeline_review is the seat's composite question ("what closes this month,
what is at risk, and quote N units"). Only the asked-for branches are
built; the planner (graph/planner.py) extends the graph from there.

    snapshot_deals ─┬─ closing_this_month ─┐
                    └─ at_risk ────────────┤
    get_item ── price_lookup ──(+ escalate_quote)─┤
                                           build_finding (needs=done) ── narrate
"""
from __future__ import annotations

from graph.engine import TaskSpec

ASKS = ("closing_this_month", "at_risk", "quote")

# Priority order for each finding section: the furthest node that ran wins.
# snapshot_deals comes after the filters so that when the read itself fails,
# the section reports that error (with its code), not just "skipped".
SECTION_SOURCES = {
    "closing_this_month": ["closing_this_month", "snapshot_deals"],
    "at_risk": ["at_risk", "snapshot_deals"],
    "quote": ["escalate_quote", "price_lookup", "get_item", "quote_refused"],
}


def pipeline_review(asks: list[str], item_id: str | None = None,
                    qty: int | None = None) -> list[TaskSpec]:
    unknown = set(asks) - set(ASKS)
    if unknown:
        raise ValueError(f"unknown asks {sorted(unknown)}; expected a subset of {ASKS}")

    tasks: list[TaskSpec] = []
    leaves: list[str] = []

    if "closing_this_month" in asks or "at_risk" in asks:
        tasks.append(TaskSpec("snapshot_deals", "snapshot_deals"))
        for name in ("closing_this_month", "at_risk"):
            if name in asks:
                tasks.append(TaskSpec(name, name, args={"snapshot": "snapshot_deals"},
                                      deps=["snapshot_deals"]))
                leaves.append(name)

    if "quote" in asks:
        if item_id:
            tasks.append(TaskSpec("get_item", "get_item", args={"item_id": item_id}))
            tasks.append(TaskSpec("price_lookup", "price_lookup",
                                  args={"item": "get_item", "qty": qty}, deps=["get_item"]))
            leaves += ["get_item", "price_lookup"]
        else:
            # Phase 1 takes the item from the CLI; lookup by name is Phase 2.
            tasks.append(TaskSpec("quote_refused", "refuse", args={
                "requested_qty": qty, "item_id": None,
                "reason": "no item specified — can't quote without knowing which item (pass --item-id)",
            }))
            leaves.append("quote_refused")

    tasks.append(TaskSpec("build_finding", "build_finding", needs="done", deps=leaves,
                          args={"asks": list(asks), "sections": SECTION_SOURCES}))
    tasks.append(TaskSpec("narrate", "narrate", args={"finding": "build_finding"},
                          deps=["build_finding"]))
    return tasks
