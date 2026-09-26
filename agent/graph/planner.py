"""RulePlanner: reacts to node outcomes by extending the graph.

Phase 1 has one rule — an unpriced quote gets an escalation node, and
build_finding is made to wait for it. More rules (re-read an incomplete
snapshot, resolve an item by name) and a constrained LLM planner come in
later phases; whatever proposes nodes, GraphStore.apply still validates
them against the registry.
"""
from __future__ import annotations

from graph.engine import GraphPatch, GraphStore, TaskSpec
from graph.outcome import ANSWERED


class RulePlanner:
    def on_outcome(self, store: GraphStore, spec: TaskSpec) -> GraphPatch | None:
        if (spec.node == "price_lookup" and spec.outcome.status == ANSWERED
                and spec.outcome.data.get("outcome") == "unpriced"
                and "escalate_quote" not in store):
            patch = GraphPatch(add=[TaskSpec("escalate_quote", "escalate_quote",
                                             args={"priced": spec.id}, deps=[spec.id])])
            if "build_finding" in store:
                patch.connect.append(("escalate_quote", "build_finding"))
            return patch
        return None
