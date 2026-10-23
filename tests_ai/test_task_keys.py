"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

A misspelt key in tasks.json used to be ignored, so its check silently
never ran and the task could still pass. Now loading the task fails and
names the key. Nothing touches the network.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from harness import runner, verifiers
from harness.task_keys import PARAMS
from harness.verifiers import validate_task

GOOD = {
    "id": "closing", "description": "d", "argv": ["Which deals close this month?"], "requires_llm": True,
    "expect": {"sections": {"closing_this_month": "answered"}},
    "verifiers": [{"name": "deal_set_matches_db", "params": {"section": "closing_this_month"}}],
}


def with_(**changes) -> dict:
    return {**json.loads(json.dumps(GOOD)), **changes}


def test_a_good_task_loads():
    validate_task(with_())


@pytest.mark.parametrize("task, bad_key", [
    (with_(descripton="typo"), "descripton"),
    (with_(expect={"secions": {"closing_this_month": "answered"}}), "secions"),
    (with_(verifiers=[{"name": "deal_set_matches_db", "params": {"secton": "closing_this_month"}}]), "secton"),
    (with_(verifiers=[{"name": "deal_set_matches_db", "param": {"section": "closing_this_month"}}]), "param"),
    (with_(verifiers=[{"name": "escalation_effect", "params": {"subject": "x"}}]), "subject"),
])
def test_a_misspelt_key_fails_and_is_named(task, bad_key):
    with pytest.raises(ValueError, match=bad_key):
        validate_task(task)


def test_a_misspelt_key_in_a_by_instance_override_fails():
    task = with_(by_instance={"keystone": {"expect": {"refusal_min": 1}}})
    with pytest.raises(ValueError, match="refusal_min"):
        validate_task(task)


def test_a_misspelt_top_level_key_in_tasks_json_fails(tmp_path):
    data = json.loads(runner.TASKS_PATH.read_text())
    data["todya"] = data.pop("today", "2026-09-28")
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="todya"):
        runner.load_tasks(path, instance="suryodaya")


@pytest.mark.parametrize("instance", ["suryodaya", "keystone"])
def test_the_real_task_set_loads(instance):
    assert runner.load_tasks(instance=instance)["tasks"]


def _params_read_by(check) -> set[str]:
    """The keys a check reads from its params argument: p["x"] and p.get("x")."""
    fn = ast.parse(Path(verifiers.__file__).read_text()).body
    node = next(n for n in fn if isinstance(n, ast.FunctionDef) and n.name == check.__name__)
    p = node.args.args[1].arg
    keys = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Subscript) and isinstance(x.value, ast.Name) and x.value.id == p \
                and isinstance(x.slice, ast.Constant):
            keys.add(x.slice.value)
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "get" \
                and isinstance(x.func.value, ast.Name) and x.func.value.id == p and x.args \
                and isinstance(x.args[0], ast.Constant):
            keys.add(x.args[0].value)
    return keys


@pytest.mark.parametrize("name", sorted(verifiers.VERIFIERS))
def test_every_param_a_check_reads_is_allowed(name):
    """Keeps PARAMS in step with the code: a key a check reads but PARAMS
    lacks would make every task that sets it fail to load."""
    assert _params_read_by(verifiers.VERIFIERS[name].check) <= PARAMS.get(name, frozenset())
