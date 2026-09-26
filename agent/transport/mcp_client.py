"""Thin JSON-RPC client for AgentSwitch's MCP surface + its login endpoint.

Transport only — no business logic. See domain/ for that.

Every failure surfaces as an MCPToolError carrying a `code` to branch on
(see docs/UNHAPPY_PATHS.md). The first four are the platform's own
`error.data.code` values; the rest are assigned here for failures that
never reach a JSON-RPC envelope:

    not_found            no such record — don't retry
    invalid_arguments    schema violation — `errors` names the field
    invalid_transition   right tool, wrong current state
    tool_not_available   tool absent from this seat — structural
    transient            network error, timeout, 429, 5xx, non-JSON body
    auth                 401 — token rejected (re-login is tried once here)
    forbidden            403 — e.g. "App 'x' is not enabled for your account"
    unknown              an error envelope without a data.code

Transport does not retry `transient` itself: whether a retry is safe
depends on whether the call writes, which only the caller knows.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

SURYODAYA = "https://agentswitch.theschoolofai.in"

NOT_FOUND = "not_found"
INVALID_ARGUMENTS = "invalid_arguments"
INVALID_TRANSITION = "invalid_transition"
TOOL_NOT_AVAILABLE = "tool_not_available"
TRANSIENT = "transient"
AUTH = "auth"
FORBIDDEN = "forbidden"
UNKNOWN = "unknown"


class MCPToolError(Exception):
    """A tool call that didn't succeed. Branch on `code`, never on the
    message text. `errors` is the platform's per-field list for
    invalid_arguments ([{"field": "/id", "message": ...}]), else empty.
    """

    def __init__(self, message: str, raw: dict | None = None,
                 code: str = UNKNOWN, errors: list | None = None):
        super().__init__(message)
        self.raw = raw or {}
        self.code = code
        self.errors = errors or []


class MCPClient:
    def __init__(self, email: str, password: str, base_url: str = SURYODAYA,
                 timeout: float = 30):
        self.base_url = base_url
        self.timeout = timeout
        self._email = email
        self._password = password
        self.token = self._login()

    def _login(self) -> str:
        body = json.dumps({"email": self._email, "password": self._password}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/auth/login", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            raw = urllib.request.urlopen(req, timeout=self.timeout).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"login failed: HTTP {e.code} {e.read()[:200]!r}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise RuntimeError(f"login failed: {self.base_url} unreachable ({e})")
        try:
            token = json.loads(raw).get("token")
        except ValueError:
            token = None
        if not token:
            raise RuntimeError(f"login response had no token: {raw[:200]!r}")
        return token

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Call one MCP tool. Returns structuredContent on success.

        Raises MCPToolError (with `code`) on any failure. On `auth`, logs
        in again and retries exactly once — a rejected token means the
        request was never processed, so this is safe even for writes.
        """
        try:
            return self._call_once(name, arguments)
        except MCPToolError as e:
            if e.code != AUTH:
                raise
            self.token = self._login()
            return self._call_once(name, arguments)

    def _call_once(self, name: str, arguments: dict | None) -> dict:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }).encode()
        req = urllib.request.Request(
            self.base_url + "/api/mcp", data=body, method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            raw = urllib.request.urlopen(req, timeout=self.timeout).read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise MCPToolError(f"{name}: HTTP 401", code=AUTH)
            if e.code == 403:
                raise MCPToolError(f"{name}: HTTP 403 {e.read()[:200]!r}", code=FORBIDDEN)
            if e.code == 429 or e.code >= 500:
                raise MCPToolError(f"{name}: HTTP {e.code}", code=TRANSIENT)
            raw = e.read()  # other 4xx may still carry a JSON-RPC envelope
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise MCPToolError(f"{name}: {e}", code=TRANSIENT)

        try:
            d = json.loads(raw)
        except ValueError:
            raise MCPToolError(f"{name}: non-JSON response {raw[:200]!r}", code=TRANSIENT)

        if "error" in d:
            err = d.get("error") or {}
            data = err.get("data") or {}
            raise MCPToolError(f"{name}: {err.get('message', '')}"[:500], d,
                               code=data.get("code", UNKNOWN),
                               errors=data.get("errors", []))
        result = d.get("result", {})
        if result.get("isError"):
            raise MCPToolError(f"{name}: {json.dumps(result)[:300]}", d, code=UNKNOWN)
        return result.get("structuredContent", {})
