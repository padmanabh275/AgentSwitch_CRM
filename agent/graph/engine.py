"""A small live task graph: nodes run when their dependencies are done, and
a planner can add nodes in reaction to what just finished.

Ported from designreview/core/dag_engine.py (itself a trim of S17Code's
live_graph), with the changes this seat needs:

- Nodes end with a NodeOutcome, not succeeded/failed. A refused or
  escalated node is a normal result; it doesn't cancel anything.
- Each node declares `needs`: "answered" (run only if every dependency
  answered; otherwise it's marked skipped, with the reason) or "done"
  (run once every dependency has finished in *any* state). "done" is the
  partial join: build_finding still runs when the quote branch fails, so
  one bad branch can't take the other two answers down with it.
- Nodes name a registry entry, never a raw MCP tool. Anything not in the
  registry is rejected when the patch is applied, so no plan or planner
  can reach a write/transition tool the registry doesn't expose.
- Transient MCP errors are retried per registry entry; writes default to
  zero retries (a timed-out create may have succeeded server-side).
- Sequential, stdlib-only. The graph is ~8 nodes and mostly a chain;
  threads and networkx bought nothing here.
- The checkpoint round-trips (to_dict/from_dict), so a later phase can
  resume a run that ended waiting on a deferred node.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from graph.outcome import ANSWERED, ERROR, SKIPPED, NodeOutcome
from graph.registry import REGISTRY, RunContext
from transport.mcp_client import TRANSIENT, MCPToolError

MAX_NODES = 30
RETRY_BACKOFF_S = 1.0
CHECKPOINT_FORMAT = "t6-graph-v1"


@dataclass
class TaskSpec:
    id: str
    node: str                          # registry name
    args: dict = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    needs: str = "answered"            # "answered" | "done"
    added_by: str = "plan"             # "plan" | "planner"
    outcome: NodeOutcome | None = None
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("id", "node", "args", "deps", "needs", "added_by",
                                           "attempts", "started_at", "finished_at")}
        d["outcome"] = self.outcome.to_dict() if self.outcome else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TaskSpec":
        d = dict(d)
        outcome = d.pop("outcome")
        spec = cls(**d)
        spec.outcome = NodeOutcome.from_dict(outcome) if outcome else None
        return spec


@dataclass
class GraphPatch:
    """The only way to change the graph. `connect` adds (dep, node) edges
    to nodes that haven't run yet — e.g. making build_finding wait for an
    escalate_quote node the planner just added."""
    add: list[TaskSpec] = field(default_factory=list)
    connect: list[tuple[str, str]] = field(default_factory=list)


class Planner(Protocol):
    def on_outcome(self, store: "GraphStore", spec: TaskSpec) -> GraphPatch | None: ...


def _has_cycle(deps: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        if any(visit(d) for d in deps[n]):
            return True
        state[n] = 2
        return False

    return any(visit(n) for n in deps)


class GraphStore:
    def __init__(self, run_id: str, checkpoint_path: Path | None = None):
        self.run_id = run_id
        self.checkpoint_path = checkpoint_path
        self.specs: dict[str, TaskSpec] = {}   # insertion-ordered: also the run order

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.specs

    def get(self, node_id: str) -> TaskSpec | None:
        return self.specs.get(node_id)

    def outcomes(self) -> dict[str, NodeOutcome]:
        return {i: s.outcome for i, s in self.specs.items() if s.outcome is not None}

    def apply(self, patch: GraphPatch) -> None:
        """Validate the whole patch, then apply it. Raises ValueError on an
        unknown registry name, duplicate id, missing dependency, an edge
        into a node that already ran, a cycle, or the node budget."""
        new_ids = [s.id for s in patch.add]
        if len(self.specs) + len(new_ids) > MAX_NODES:
            raise ValueError(f"patch would exceed the {MAX_NODES}-node budget")
        for spec in patch.add:
            if spec.node not in REGISTRY:
                raise ValueError(f"node {spec.id}: {spec.node!r} is not in the registry")
            if spec.id in self.specs or new_ids.count(spec.id) > 1:
                raise ValueError(f"duplicate node id {spec.id!r}")
            if spec.needs not in ("answered", "done"):
                raise ValueError(f"node {spec.id}: needs must be 'answered' or 'done'")
            for dep in spec.deps:
                if dep not in self.specs and dep not in new_ids:
                    raise ValueError(f"node {spec.id} depends on unknown node {dep!r}")
        for dep, node_id in patch.connect:
            target = self.specs.get(node_id)
            if target is None or (dep not in self.specs and dep not in new_ids):
                raise ValueError(f"connect {dep}->{node_id}: unknown node")
            if target.outcome is not None or target.started_at is not None:
                raise ValueError(f"connect {dep}->{node_id}: {node_id} already ran")

        # Check the cycle on the proposed dependency map before touching the
        # store, so a rejected patch leaves the graph exactly as it was.
        proposed = {i: list(s.deps) for i, s in self.specs.items()}
        proposed.update({s.id: list(s.deps) for s in patch.add})
        for dep, node_id in patch.connect:
            if dep not in proposed[node_id]:
                proposed[node_id].append(dep)
        if _has_cycle(proposed):
            raise ValueError("patch introduces a dependency cycle")

        for spec in patch.add:
            self.specs[spec.id] = spec
        for node_id, deps in proposed.items():
            self.specs[node_id].deps = deps
        self.checkpoint()

    def next_ready(self) -> TaskSpec | None:
        """Mark any node whose `needs` can no longer be met as skipped, then
        return the first pending node whose dependencies are satisfied."""
        progressed = True
        while progressed:
            progressed = False
            for spec in self.specs.values():
                if spec.outcome is not None:
                    continue
                deps = [self.specs[d] for d in spec.deps]
                if any(d.outcome is None for d in deps):
                    continue
                blocking = [d for d in deps if d.outcome.status != ANSWERED]
                if spec.needs == "answered" and blocking:
                    b = blocking[0]
                    spec.outcome = NodeOutcome(
                        SKIPPED, reason=f"dependency {b.id} was {b.outcome.status}"
                                        + (f": {b.outcome.reason}" if b.outcome.reason else ""))
                    spec.finished_at = time.time()
                    progressed = True
                    continue
                return spec
        return None

    def checkpoint(self) -> None:
        if not self.checkpoint_path:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(json.dumps(self.to_dict(), indent=2, default=str))

    def to_dict(self) -> dict:
        return {"format": CHECKPOINT_FORMAT, "run_id": self.run_id,
                "nodes": [s.to_dict() for s in self.specs.values()]}

    @classmethod
    def from_dict(cls, d: dict, checkpoint_path: Path | None = None) -> "GraphStore":
        if d.get("format") != CHECKPOINT_FORMAT:
            raise ValueError(f"unsupported checkpoint format {d.get('format')!r}")
        store = cls(d["run_id"], checkpoint_path)
        for n in d["nodes"]:
            spec = TaskSpec.from_dict(n)
            store.specs[spec.id] = spec
        return store


class Engine:
    def __init__(self, ctx: RunContext, planner: Planner | None = None,
                 checkpoint_path: Path | None = None):
        self.ctx = ctx
        self.planner = planner
        self.checkpoint_path = checkpoint_path

    def run(self, run_id: str, tasks: list[TaskSpec]) -> GraphStore:
        store = GraphStore(run_id, self.checkpoint_path)
        store.apply(GraphPatch(add=tasks))
        return self.resume(store)

    def resume(self, store: GraphStore) -> GraphStore:
        """Run every node that can still run. A planner exception is not
        caught: a broken planner should fail the run loudly, not quietly
        produce a half-built graph (the caller still saves the record)."""
        while (spec := store.next_ready()) is not None:
            self._execute(store, spec)
            store.checkpoint()
            if self.planner:
                patch = self.planner.on_outcome(store, spec)
                if patch and (patch.add or patch.connect):
                    for new in patch.add:
                        new.added_by = "planner"
                    store.apply(patch)
        store.checkpoint()
        return store

    def _execute(self, store: GraphStore, spec: TaskSpec) -> None:
        entry = REGISTRY[spec.node]
        spec.started_at = time.time()
        inputs = store.outcomes()
        while True:
            spec.attempts += 1
            try:
                result = entry.fn(self.ctx, spec.args, inputs)
                spec.outcome = (result if isinstance(result, NodeOutcome)
                                else NodeOutcome.from_domain(result))
                break
            except MCPToolError as e:
                if e.code == TRANSIENT and spec.attempts <= entry.retries:
                    time.sleep(RETRY_BACKOFF_S * 2 ** (spec.attempts - 1))
                    continue
                spec.outcome = NodeOutcome(ERROR, reason=str(e), error_code=e.code)
                break
            except Exception as e:  # a bug in a node: record it, keep the other branches
                spec.outcome = NodeOutcome(ERROR, reason=f"{type(e).__name__}: {e}",
                                           error_code="internal")
                break
        spec.finished_at = time.time()
