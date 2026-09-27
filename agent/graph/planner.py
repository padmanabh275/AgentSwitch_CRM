"""RulePlanner: reacts to node outcomes by extending the graph.

Rules:
- an unpriced quote gets an escalation node, and build_finding waits for it;
- an incomplete deal snapshot gets one full re-read, and both lists wait
  for it (they read the re-read if it answered, else the original).

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

        if (spec.node == "price_lookup" and data.get("outcome") == "unpriced"
                and "escalate_quote" not in store):
            return GraphPatch(
                add=[TaskSpec("escalate_quote", "escalate_quote",
                              args={"priced": spec.id}, deps=[spec.id])],
                connect=[("escalate_quote", i) for i in _waiting(store, ("build_finding",))])

        if (spec.id == "snapshot_deals" and data.get("pagination_complete") is False
                and "reread_deals" not in store):
            return GraphPatch(
                add=[TaskSpec("reread_deals", "reread_deals",
                              args={"previous": spec.id}, deps=[spec.id])],
                connect=[("reread_deals", i)
                         for i in _waiting(store, ("closing_this_month", "at_risk"))])
        return None
