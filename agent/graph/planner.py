"""RulePlanner: reacts to node outcomes by extending the graph.

Rules:
- an unpriced quote gets an escalation node, and build_finding waits for it
  — unless it's on a named deal: then draft_for_approval (an unpriced
  Quotation) and, once drafted, submit_for_approval (margin approval);
- an incomplete deal snapshot gets one full re-read, and both lists wait
  for it (they read the re-read if it answered, else the original);
- a priced quote on a named deal (get_deal in the plan) gets
  create_quotation, and a drafted quotation gets send_quotation, each
  with build_finding waiting for it.

Whatever proposes nodes, GraphStore.apply still validates them against the
registry. A constrained LLM planner is Phase 3.
"""
from __future__ import annotations

from graph.engine import GraphPatch, GraphStore, TaskSpec
from graph.outcome import ANSWERED


def _waiting(store: GraphStore, node_ids: tuple[str, ...]) -> list[str]:
    """The given nodes that exist and haven't started — safe to add deps to."""
    return [i for i in node_ids
            if i in store and store.get(i).started_at is None and store.get(i).outcome is None]


class RulePlanner:
    def on_outcome(self, store: GraphStore, spec: TaskSpec) -> GraphPatch | None:
        if spec.outcome.status != ANSWERED:
            return None
        data = spec.outcome.data

        # Unpriced on a named deal: the quotation goes to margin approval
        # (the seat has no AgentEscalation.create since 2026-10-15).
        if (spec.node == "price_lookup" and data.get("outcome") == "unpriced"
                and "get_deal" in store and "draft_for_approval" not in store):
            return GraphPatch(
                add=[TaskSpec("draft_for_approval", "draft_for_approval",
                              args={"deal": "get_deal", "priced": spec.id},
                              deps=["get_deal", spec.id])],
                connect=[("draft_for_approval", i) for i in _waiting(store, ("build_finding",))])

        if (spec.node == "draft_for_approval" and data.get("outcome") in ("drafted", "would_create")
                and "submit_for_approval" not in store):
            return GraphPatch(
                add=[TaskSpec("submit_for_approval", "submit_for_approval",
                              args={"drafted": spec.id}, deps=[spec.id])],
                connect=[("submit_for_approval", i) for i in _waiting(store, ("build_finding",))])

        if (spec.node == "price_lookup" and data.get("outcome") == "unpriced"
                and "get_deal" not in store and "escalate_quote" not in store):
            return GraphPatch(
                add=[TaskSpec("escalate_quote", "escalate_quote",
                              args={"priced": spec.id}, deps=[spec.id])],
                connect=[("escalate_quote", i) for i in _waiting(store, ("build_finding",))])

        if (spec.node == "price_lookup" and data.get("outcome") == "quoted"
                and "get_deal" in store and "create_quotation" not in store):
            return GraphPatch(
                add=[TaskSpec("create_quotation", "create_quotation",
                              args={"deal": "get_deal", "priced": spec.id},
                              deps=["get_deal", spec.id])],
                connect=[("create_quotation", i) for i in _waiting(store, ("build_finding",))])

        if (spec.node == "create_quotation" and data.get("outcome") in ("drafted", "would_create")
                and "send_quotation" not in store):
            return GraphPatch(
                add=[TaskSpec("send_quotation", "send_quotation",
                              args={"drafted": spec.id}, deps=[spec.id])],
                connect=[("send_quotation", i) for i in _waiting(store, ("build_finding",))])

        if (spec.id == "snapshot_deals" and data.get("pagination_complete") is False
                and "reread_deals" not in store):
            return GraphPatch(
                add=[TaskSpec("reread_deals", "reread_deals",
                              args={"previous": spec.id}, deps=[spec.id])],
                connect=[("reread_deals", i)
                         for i in _waiting(store, ("closing_this_month", "at_risk"))])
        return None
