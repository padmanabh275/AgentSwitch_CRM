"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

tasks.json is written for Suryodaya; a task's by_instance entry swaps in
what differs on another instance (an item ref that exists there). The
instance comes from the same env client_from_env reads, so the harness's
ground-truth login, the agent, and the task set all agree on it.
"""
from __future__ import annotations

import pytest

from harness import runner, verifiers
from transport.mcp_client import KEYSTONE, SURYODAYA, instance_from_env

TASK = {
    "id": "quote_x",
    "argv": ["--asks", "quote", "--item", "ST-VICE-150", "--qty", "500"],
    "requires_llm": False,
    "verifiers": [{"name": "quote_matches_db", "params": {"ref": "ST-VICE-150", "qty": 500}}],
    "by_instance": {"keystone": {
        "argv": ["--asks", "quote", "--item", "FG-HYD-0400", "--qty", "500"],
        "verifiers": [{"name": "quote_matches_db", "params": {"ref": "FG-HYD-0400", "qty": 500}}],
    }},
}


@pytest.mark.parametrize("env, want", [
    ({}, "suryodaya"),
    ({"AS": KEYSTONE}, "keystone"),
    ({"AS": KEYSTONE + "/"}, "keystone"),
    ({"AS": "https://elsewhere.example"}, None),
    ({"AGENTSWITCH_INSTANCE": "keystone", "AS": SURYODAYA}, "keystone"),
    ({"AGENTSWITCH_TOKEN": "t", "AGENTSWITCH_BASE_URL": KEYSTONE, "AS": SURYODAYA}, "keystone"),
])
def test_instance_from_env(env, want):
    assert instance_from_env(env) == want


def test_override_replaces_fields_on_its_instance():
    t = runner.for_instance(TASK, "keystone")
    assert "FG-HYD-0400" in t["argv"]
    assert t["verifiers"][0]["params"]["ref"] == "FG-HYD-0400"
    assert t["instance"] == "keystone" and "by_instance" not in t
    assert t["requires_llm"] is False


@pytest.mark.parametrize("instance", ["suryodaya", None])
def test_other_instances_keep_the_defaults(instance):
    t = runner.for_instance(TASK, instance)
    assert t["argv"] == TASK["argv"] and t["verifiers"] == TASK["verifiers"]
    assert "by_instance" not in t


def test_override_cannot_change_what_kind_of_task_it_is():
    bad = {**TASK, "by_instance": {"keystone": {"requires_write": True}}}
    with pytest.raises(ValueError, match="can't override"):
        verifiers.validate_task(bad)


def test_override_verifiers_are_validated():
    bad = {**TASK, "by_instance": {"keystone": {"verifiers": [{"name": "no_such_check"}]}}}
    with pytest.raises(ValueError, match="unknown verifier"):
        verifiers.validate_task(bad)


@pytest.mark.parametrize("instance", ["suryodaya", "keystone"])
def test_real_task_file_loads_for_each_instance(instance):
    data = runner.load_tasks(instance=instance)
    assert data["instance"] == instance
    for t in data["tasks"]:
        assert "by_instance" not in t
        # a quote task's request and its check name the same item
        refs = [s["params"]["ref"] for s in t.get("verifiers") or [] if s["name"] == "quote_matches_db"]
        if refs and "--item" in t["argv"]:
            assert t["argv"][t["argv"].index("--item") + 1] == refs[0], t["id"]
