"""The run's writes to our own agent space on AgentSwitch.

All of these are skipped under --dry-run, and none of them can fail a run:
the local TaskRun is saved first, and a platform write that fails becomes
a warning on it. AgentMessage has no create tool for this seat, so the
transcript lives in the local run directory; the platform gets the
session (with tool-call count and run id) and the finding.
"""
from __future__ import annotations

import json

from domain.escalation import T6_PREFIX
from harness.run_record import TaskRun
from transport.mcp_client import MCPClient

SESSION_TITLE = f"{T6_PREFIX}Sales pipeline agent run"


def open_session(client: MCPClient) -> str:
    """session_id is a foreign key to a real AgentSession — AgentMemory.create
    and AgentEscalation.create both reject a bare uuid4 (found live)."""
    return client.call("AgentSession.create", {"channel": "api", "title": SESSION_TITLE})["id"]


def record_run(client: MCPClient, run: TaskRun) -> list[str]:
    """Store the finding and annotate the session. Returns warnings."""
    if run.dry_run or not run.session_id:
        return []
    warnings = []
    if run.finding is not None:
        try:
            client.call("AgentMemory.create", {
                "session_id": run.session_id,
                "category": "context",
                "content": json.dumps(run.finding, default=str),
            })
        except Exception as e:
            warnings.append(f"AgentMemory.create failed: {e}")
    try:
        client.call("AgentSession.update", {
            "id": run.session_id,
            "total_tool_calls": len(run.steps),
            "metadata": json.dumps({"run_id": run.run_id, "task_id": run.task_id,
                                    "ended": run.ended}),
        })
    except Exception as e:
        warnings.append(f"AgentSession.update failed: {e}")
    return warnings
