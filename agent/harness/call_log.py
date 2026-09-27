"""LoggedClient: every MCP tool call this run made, as the transport saw it.

Wraps MCPClient.call and appends one JSON line per call to
runs/<run_id>/calls.jsonl — tool name, arguments, how it ended and how
long it took. This is the scorer's source for "which tools did the run
actually touch" (e.g. "a dry run made no create/update call"), so that
claim no longer rests on the agent's own report of itself.

Only tool arguments are logged, never the bearer token or the login body.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from transport.mcp_client import MCPToolError


class LoggedClient:
    def __init__(self, client, path: Path):
        self._client = client
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _append(self, entry: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.count += 1
        entry = {"seq": self.count, "tool": name, "args": arguments or {},
                 "started_at": time.time()}
        t0 = time.time()
        try:
            result = self._client.call(name, arguments)
        except MCPToolError as e:
            self._append({**entry, "ok": False, "error_code": e.code,
                          "error": str(e)[:300], "seconds": round(time.time() - t0, 3)})
            raise
        except Exception as e:
            self._append({**entry, "ok": False, "error_code": "internal",
                          "error": f"{type(e).__name__}: {e}"[:300],
                          "seconds": round(time.time() - t0, 3)})
            raise
        self._append({**entry, "ok": True, "error_code": None,
                      "result_id": result.get("id") if isinstance(result, dict) else None,
                      "seconds": round(time.time() - t0, 3)})
        return result


def read_calls(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
