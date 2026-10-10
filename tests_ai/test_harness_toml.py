"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

agentswitch-harness.toml must stay within the platform's contract (keys,
instances, 1-30 min timeout) and agree with hosted.py: the run command's
file exists, its budget leaves margin under the timeout, and results points
where hosted.py writes.
"""
from __future__ import annotations

import shlex

import pytest

from config import REPO_DIR
from harness import hosted

tomllib = pytest.importorskip("tomllib")   # stdlib from 3.11

TOML = tomllib.loads((REPO_DIR / "agentswitch-harness.toml").read_text(encoding="utf-8"))


def run_flag(name: str):
    argv = shlex.split(TOML["run"])
    return argv[argv.index(name) + 1] if name in argv else None


def test_has_exactly_the_platform_keys():
    assert set(TOML) == {"install", "run", "results", "instances", "timeout_minutes"}


def test_instances_and_timeout_are_valid():
    assert TOML["instances"] and set(TOML["instances"]) <= {"suryodaya", "keystone"}
    assert 1 <= TOML["timeout_minutes"] <= 30


def test_run_needs_no_shell_and_its_file_exists():
    argv = shlex.split(TOML["run"])
    assert argv[0] == "python"
    assert not {"cd", "&&", ";", "|"} & set(argv)
    assert (REPO_DIR / argv[1]).is_file()


def test_budget_leaves_margin_under_the_timeout():
    budget = float(run_flag("--budget-minutes") or hosted.BUDGET_MINUTES)
    assert budget <= TOML["timeout_minutes"] - 3


def test_results_path_is_where_hosted_writes():
    assert (REPO_DIR / TOML["results"]).resolve() == hosted.RESULTS_PATH.resolve()


def test_install_file_exists():
    argv = shlex.split(TOML["install"])
    if "-r" in argv:
        assert (REPO_DIR / argv[argv.index("-r") + 1]).is_file()
