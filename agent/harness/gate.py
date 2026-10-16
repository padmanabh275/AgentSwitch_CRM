"""A pause point the harness can hold a run at. A no-op unless the harness asks.

The concurrency task needs another writer to change the deal *between* the
agent's first read and its write. The agent runs in a subprocess, so the
runner sets T6_HARNESS_GATE_DIR; at a named gate the agent writes
<dir>/<name>.ready and waits for <dir>/<name>.go. The runner makes its
edit, records it in interference.json, then writes .go.

Without the variable (every normal run) wait_at returns at once. If .go
never comes, the run carries on after GATE_TIMEOUT_S rather than hang.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ENV = "T6_HARNESS_GATE_DIR"
GATE_TIMEOUT_S = 120.0
POLL_S = 0.2


def wait_at(name: str, timeout_s: float = GATE_TIMEOUT_S) -> dict | None:
    """Block at gate `name` while the harness holds it. Returns what
    happened, or None when no harness gate is set."""
    gate_dir = os.environ.get(ENV)
    if not gate_dir:
        return None
    d = Path(gate_dir)
    d.mkdir(parents=True, exist_ok=True)
    ready_at = time.time()
    (d / f"{name}.ready").write_text(json.dumps({"at": ready_at}), encoding="utf-8")
    go = d / f"{name}.go"
    deadline = ready_at + timeout_s
    while time.time() < deadline:
        if go.exists():
            return {"gate": name, "released": True, "waited_s": round(time.time() - ready_at, 2)}
        time.sleep(POLL_S)
    return {"gate": name, "released": False, "waited_s": round(time.time() - ready_at, 2)}
