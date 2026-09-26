"""Escalation filing (AgentEscalation.create). Zero LLM."""
from __future__ import annotations

from transport.mcp_client import MCPClient

ASSIGNEE_EMAIL = "meera.kulkarni@suryodaya.in"  # only entry in
# GET /api/agent-governance/escalations/assignees — see design doc.


def file_escalation(client: MCPClient, session_id: str, reason: str,
                     reason_code: str = "other", subject: str | None = None,
                     party_id: str | None = None) -> dict:
    """Wraps AgentEscalation.create. reason_code defaults to "other" because
    the enum has no value for a cross-app capability gap (bug candidate E1) —
    the single most predictable escalation reason on a seat-scoped platform.
    """
    args = {"session_id": session_id, "reason": reason, "reason_code": reason_code}
    if subject:
        args["subject"] = subject
    if party_id:
        args["party_id"] = party_id
    result = client.call("AgentEscalation.create", args)
    return {"escalation_id": result.get("id"), "assignee": ASSIGNEE_EMAIL}

