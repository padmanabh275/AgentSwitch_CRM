"""graph/engine.py: patch validation and dependency handling in GraphStore."""
from __future__ import annotations

import pytest

from graph.engine import MAX_NODES, GraphPatch, GraphStore, TaskSpec
from graph.outcome import ANSWERED, REFUSED, SKIPPED, NodeOutcome


def base_store():
    """snap -> risk, nothing run yet. No checkpoint path, so nothing is written."""
    store = GraphStore("t", None)
    store.apply(GraphPatch(add=[
        TaskSpec("snap", "snapshot_deals"),
        TaskSpec("risk", "at_risk", deps=["snap"]),
    ]))
    return store


def shape(store):
    return {i: list(s.deps) for i, s in store.specs.items()}


# --- apply ------------------------------------------------------------------

def test_valid_patch_is_applied():
    store = base_store()
    store.apply(GraphPatch(add=[TaskSpec("month", "closing_this_month", deps=["snap"])]))
    assert shape(store) == {"snap": [], "risk": ["snap"], "month": ["snap"]}


@pytest.mark.parametrize("patch", [
    GraphPatch(add=[TaskSpec("x", "no_such_node")]),
    GraphPatch(add=[TaskSpec("snap", "snapshot_deals")]),
    GraphPatch(add=[TaskSpec("x", "at_risk"), TaskSpec("x", "at_risk")]),
    GraphPatch(add=[TaskSpec("x", "at_risk", deps=["ghost"])]),
    GraphPatch(add=[TaskSpec("a", "at_risk", deps=["b"]), TaskSpec("b", "at_risk", deps=["a"])]),
    GraphPatch(add=[TaskSpec("x", "at_risk", needs="sometimes")]),
    GraphPatch(add=[TaskSpec(f"n{i}", "snapshot_deals") for i in range(MAX_NODES - 1)]),
    GraphPatch(connect=[("risk", "snap")]),
], ids=["unknown_registry_name", "duplicate_existing_id", "duplicate_in_patch",
        "unknown_dependency", "cycle", "bad_needs", "over_node_budget", "connect_makes_cycle"])
def test_bad_patch_is_rejected_and_store_untouched(patch):
    store = base_store()
    before = shape(store)
    with pytest.raises(ValueError):
        store.apply(patch)
    assert shape(store) == before


def test_connect_into_a_node_that_already_ran_is_rejected():
    store = base_store()
    store.get("snap").outcome = NodeOutcome(ANSWERED)
    before = shape(store)
    with pytest.raises(ValueError, match="already ran"):
        store.apply(GraphPatch(add=[TaskSpec("snap2", "snapshot_deals")],
                               connect=[("snap2", "snap")]))
    assert shape(store) == before


# --- next_ready -------------------------------------------------------------

def test_next_ready_returns_the_first_node_with_no_pending_deps():
    store = base_store()
    assert store.next_ready().id == "snap"


def test_dependent_waits_until_its_dependency_finishes():
    store = base_store()
    store.get("snap").outcome = NodeOutcome(ANSWERED)
    assert store.next_ready().id == "risk"


def test_failed_dependency_skips_a_node_that_needs_answered():
    store = base_store()
    store.get("snap").outcome = NodeOutcome(REFUSED, reason="nope")

    assert store.next_ready() is None
    risk = store.get("risk").outcome
    assert risk.status == SKIPPED
    assert risk.reason == "dependency snap was refused: nope"


def test_failed_dependency_still_runs_a_node_that_needs_done():
    store = GraphStore("t", None)
    store.apply(GraphPatch(add=[
        TaskSpec("snap", "snapshot_deals"),
        TaskSpec("risk", "at_risk", deps=["snap"], needs="done"),
    ]))
    store.get("snap").outcome = NodeOutcome(REFUSED, reason="nope")
    assert store.next_ready().id == "risk"


def test_checkpoint_round_trip():
    store = base_store()
    store.get("snap").outcome = NodeOutcome(ANSWERED, data={"deals": []})
    copy = GraphStore.from_dict(store.to_dict())
    assert shape(copy) == shape(store)
    assert copy.get("snap").outcome == store.get("snap").outcome
