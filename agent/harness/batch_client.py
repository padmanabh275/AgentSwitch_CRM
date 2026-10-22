"""The runner's platform client for one batch.

Every platform call costs ~1.6s whatever the tool (median of 160, live
2026-10-21), so reads that can't change within a batch are made once:
tools/list (this seat's tool set), and the fixture lookups that go through
memo() (the first party, the item list). Everything else, including every
write and every ground-truth record read, goes straight to the platform.

The agent runs in its own process with its own client, so none of this
reaches what the agent sees. Safe to share between threads: the wrapped
MCPClient keeps no per-request state, and a memo is filled once under a lock.
"""
from __future__ import annotations

import copy
import threading
from typing import Callable


class BatchClient:
    def __init__(self, client):
        self._client = client
        self._memo: dict[str, object] = {}
        self._lock = threading.Lock()

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self._client.call(name, arguments)

    def list_tools(self) -> list[dict]:
        return self.memo("tools/list", self._client.list_tools)

    def memo(self, key: str, fetch: Callable[[], object]):
        """fetch() the first time `key` is asked for, then a copy of that
        result. A fetch that raises is not stored, so the next ask retries."""
        with self._lock:
            if key not in self._memo:
                self._memo[key] = fetch()
            return copy.deepcopy(self._memo[key])

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def memo(client, key: str, fetch: Callable[[], object]):
    """client.memo when the client is a BatchClient, else fetch() every time."""
    return client.memo(key, fetch) if isinstance(client, BatchClient) else fetch()
