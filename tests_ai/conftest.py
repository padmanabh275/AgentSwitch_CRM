"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-04).

Not the team's hand-written tests and not claimed as such: the course scores
a test written by Claude or Codex as zero. See tests_ai/README.md.

Shared test doubles. Every test here is offline: nothing reaches the platform."""
from __future__ import annotations

import pytest

from transport.mcp_client import MCPToolError


class FakeClient:
    """Stands in for MCPClient. `handlers` maps a tool name to either a
    function (args -> result) or a list of results returned in order.
    A result that is an MCPToolError is raised instead of returned.
    Every call is recorded in `calls` as (tool, args).
    """

    def __init__(self, handlers: dict | None = None):
        self.handlers = dict(handlers or {})
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool not in self.handlers:
            raise AssertionError(f"unexpected tool call {tool} {args}")
        handler = self.handlers[tool]
        result = handler(args) if callable(handler) else handler.pop(0)
        if isinstance(result, MCPToolError):
            raise result
        return result

    def tools_called(self) -> list[str]:
        return [tool for tool, _ in self.calls]


@pytest.fixture
def fake_client():
    return FakeClient
