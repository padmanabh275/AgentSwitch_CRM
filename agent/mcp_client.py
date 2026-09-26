"""Thin JSON-RPC client for AgentSwitch's MCP surface + its login endpoint.

Transport only — no business logic. See domain.py for that.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

SURYODAYA = "https://agentswitch.theschoolofai.in"


class MCPToolError(Exception):
    """A tool call came back as an error — either JSON-RPC level or the
    tool's own isError:true. Callers that expect "not found" to be a normal
    outcome (get_deal, get_lead) catch this; callers that don't (the
    paginated list calls) let it propagate as a genuine failure.
    """

    def __init__(self, message: str, raw: dict):
        super().__init__(message)
        self.raw = raw


class MCPClient:
    def __init__(self, email: str, password: str, base_url: str = SURYODAYA):
        self.base_url = base_url
        self.token = self._login(email, password)

    def _login(self, email: str, password: str) -> str:
        body = json.dumps({"email": email, "password": password}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/auth/login", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            raw = urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"login failed: HTTP {e.code} {e.read()[:200]!r}")
        token = json.loads(raw).get("token")
        if not token:
            raise RuntimeError(f"login response had no token: {raw[:200]!r}")
        return token

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Call one MCP tool. Returns structuredContent on success.

        Raises MCPToolError for both JSON-RPC-level errors and tool-level
        isError:true — the two ways this platform reports "that didn't work".
        """
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }).encode()
        req = urllib.request.Request(
            self.base_url + "/api/mcp", data=body, method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            raw = urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        d = json.loads(raw)
        if "error" in d:
            raise MCPToolError(f"{name}: {json.dumps(d['error'])[:300]}", d)
        result = d.get("result", {})
        if result.get("isError"):
            raise MCPToolError(f"{name}: {json.dumps(result)[:300]}", d)
        return result.get("structuredContent", {})
