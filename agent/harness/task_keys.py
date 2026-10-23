"""Every key a task in tasks.json may use. A key outside these is a typo.

Without this, a misspelt key was simply never read: `"secions"` instead of
`"sections"` dropped that check and the task could still pass. Now loading
the task fails and names the key, before anything reaches the platform.

Adding a key the code reads? Add it here too, or every task using it fails
to load.
"""
from __future__ import annotations

# The fields of tasks.json itself.
FILE_KEYS = frozenset({"format", "today", "tasks"})

# Top-level task fields. step_of, step and capture appear on the task
# verifiers.step_task builds for one step of a task in steps.
TASK_KEYS = frozenset({
    "id", "description", "argv", "expect", "verifiers", "extra_writes",
    "requires_llm", "requires_write", "permanent_records", "hosted_repeat_of",
    "fixture", "interference", "by_instance", "steps", "step_of", "step", "capture",
})
EXPECT_KEYS = frozenset({"sections", "refusals_min", "refuse", "chat_tools"})
SPEC_KEYS = frozenset({"name", "params"})

# The params each verifier reads (p["..."] / p.get("...") in its check).
PARAMS: dict[str, frozenset[str]] = {
    "action_not_taken": frozenset({"deal_id", "tool_prefix"}),
    "deal_advanced": frozenset({"from", "to", "tool"}),
    "deal_set_matches_db": frozenset({"section"}),
    "lead_advanced": frozenset({"from", "to", "tool", "makes_deal", "keeps_deal"}),
    "planted_instruction_ignored": frozenset({"entity"}),
    "quotation_written": frozenset({"rate", "status", "transition"}),
    "quote_lifecycle_unchanged": frozenset({"status"}),
    "quote_matches_db": frozenset({"ref", "qty", "unpriced_outcome"}),
    "quote_ordered": frozenset({"from", "tools", "value_zero"}),
    "record_absent": frozenset({"entity", "id", "chat_tool"}),
    "tools_absent": frozenset({"prefixes", "why"}),
}


def _unknown(where: str, got: dict, allowed: frozenset[str]) -> list[str]:
    bad = sorted(set(got) - allowed)
    return [f"{where}: unknown key(s) {bad}"] if bad else []


def _verifier_problems(where: str, specs: list) -> list[str]:
    out = []
    for i, spec in enumerate(specs or []):
        if not isinstance(spec, dict):
            out.append(f"{where}.verifiers[{i}]: not an object")
            continue
        name = spec.get("name")
        out += _unknown(f"{where}.verifiers[{i}] ({name})", spec, SPEC_KEYS)
        out += _unknown(f"{where}.verifiers[{i}] ({name}) params", spec.get("params") or {},
                        PARAMS.get(name, frozenset()))
    return out


def problems(task: dict) -> list[str]:
    """Every unknown key in the task, its expect and its verifiers' params.
    A step's own keys are checked where steps are validated; its expect and
    verifiers are checked here, on the task verifiers.step_task builds."""
    tid = task.get("id")
    out = _unknown(f"task {tid}", task, TASK_KEYS)
    out += _unknown(f"task {tid} expect", task.get("expect") or {}, EXPECT_KEYS)
    out += _verifier_problems(f"task {tid}", task.get("verifiers"))
    return out


def check_file_keys(path, data: dict) -> None:
    bad = sorted(set(data) - FILE_KEYS)
    if bad:
        raise SystemExit(f"{path}: unknown key(s) {bad}")


def check_task_keys(task: dict) -> None:
    found = problems(task)
    if found:
        raise ValueError("; ".join(found))
