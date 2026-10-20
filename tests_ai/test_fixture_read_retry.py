"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

harness/fixtures.py retries its reads (get, list, tools/list) on a transient
error and never its writes: a timed-out create may have landed, so a retry
could leave a second test record. Live 2026-10-20 a fixture's Deal.get timed
out and failed the task before the agent ran. Nothing touches the network.
"""
from __future__ import annotations

import pytest

from harness import fixtures
from transport.mcp_client import NOT_FOUND, TRANSIENT, MCPToolError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(fixtures.time, "sleep", slept.append)
    return slept


class Platform:
    """Fails the named tool `fail` times with `code`, then answers."""

    def __init__(self, tool, fail, code=TRANSIENT):
        self.tool, self.fail, self.code, self.calls = tool, fail, code, []

    def list_tools(self):
        self.calls.append("tools/list")
        if self.tool == "tools/list" and self.fail:
            self.fail -= 1
            raise MCPToolError("tools/list: timed out", code=self.code)
        return [{"name": "Lead.disqualify.new.disqualified"}]

    def call(self, tool, args=None):
        self.calls.append(tool)
        if tool == self.tool and self.fail:
            self.fail -= 1
            raise MCPToolError(f"{tool}: timed out", code=self.code)
        if tool == "Party.list":
            return {"data": [{"id": "p1", "company_id": "c1"}]}
        if tool == "Lead.create":
            return {"id": "l1", "created_by": "me"}
        if tool == "Lead.get":
            return {"id": "l1", "status": "new", "updated_at": "t0"}
        return {}


def test_a_transient_read_is_retried_with_backoff(no_sleep):
    p = Platform("Party.list", fail=2)
    fx = fixtures.create(p, "new_lead", "r1")
    assert fx["lead_id"] == "l1"
    assert p.calls.count("Party.list") == 3
    assert no_sleep == [fixtures.READ_BACKOFF_S, fixtures.READ_BACKOFF_S * 2]


def test_a_read_that_keeps_timing_out_still_fails():
    p = Platform("Party.list", fail=fixtures.READ_ATTEMPTS)
    with pytest.raises(MCPToolError, match="timed out"):
        fixtures.create(p, "new_lead", "r1")
    assert p.calls.count("Party.list") == fixtures.READ_ATTEMPTS
    assert "Lead.create" not in p.calls


def test_a_non_transient_read_error_is_not_retried():
    p = Platform("Party.list", fail=1, code=NOT_FOUND)
    with pytest.raises(MCPToolError):
        fixtures.create(p, "new_lead", "r1")
    assert p.calls.count("Party.list") == 1


def test_a_timed_out_write_is_never_retried():
    p = Platform("Lead.create", fail=1)
    with pytest.raises(MCPToolError, match="timed out"):
        fixtures.create(p, "new_lead", "r1")
    assert p.calls.count("Lead.create") == 1


def test_the_catalogue_read_is_retried():
    p = Platform("tools/list", fail=1)
    out = fixtures.retire(p, {"kind": "new_lead", "lead_id": "l1"})
    assert out["tool"] == "Lead.disqualify.new.disqualified" and "error" not in out
    assert p.calls.count("tools/list") == 2


def test_read_helper_refuses_a_write():
    with pytest.raises(ValueError, match="not a read"):
        fixtures._read(Platform("x", 0), "Lead.create", {})
