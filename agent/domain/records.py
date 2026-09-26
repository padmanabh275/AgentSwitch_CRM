"""Generic single-record reads (flexibility valve). Zero LLM."""
from __future__ import annotations

from transport.mcp_client import NOT_FOUND, MCPClient, MCPToolError


def _get(client: MCPClient, entity: str, record_id: str) -> dict:
    """<entity>.get, with only a genuine not_found turned into a refusal.

    Every other failure (auth, transient, tool_not_available, ...) is
    raised, so it can never masquerade as "that record doesn't exist" —
    the refusal is a claim about the data, and only not_found backs it.
    """
    key = entity.lower()
    try:
        record = client.call(f"{entity}.get", {"id": record_id})
    except MCPToolError as e:
        if e.code != NOT_FOUND:
            raise
        return {"outcome": "refused", "id": record_id,
                "reason": f"{key} {record_id} does not exist"}
    return {"outcome": "answered", "id": record_id, key: record}


def get_deal(client: MCPClient, deal_id: str) -> dict:
    """Generic read (flexibility valve). Also the pure-refuse showcase: a
    nonexistent deal ID gets a plain refusal, never a guessed answer.
    """
    return _get(client, "Deal", deal_id)


def get_lead(client: MCPClient, lead_id: str) -> dict:
    """Generic read (flexibility valve). Same nonexistent-ID refusal as get_deal."""
    return _get(client, "Lead", lead_id)
