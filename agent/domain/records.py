"""Generic single-record reads (flexibility valve). Zero LLM."""
from __future__ import annotations

from transport.mcp_client import MCPClient, MCPToolError


def get_deal(client: MCPClient, deal_id: str) -> dict:
    """Generic read (flexibility valve). Also the pure-refuse showcase: a
    nonexistent deal ID gets a plain refusal, never a guessed answer.
    """
    try:
        deal = client.call("Deal.get", {"id": deal_id})
    except MCPToolError:
        return {"outcome": "refused", "id": deal_id,
                "reason": f"deal {deal_id} does not exist"}
    return {"outcome": "answered", "id": deal_id, "deal": deal}


def get_lead(client: MCPClient, lead_id: str) -> dict:
    """Generic read (flexibility valve). Same nonexistent-ID refusal as get_deal."""
    try:
        lead = client.call("Lead.get", {"id": lead_id})
    except MCPToolError:
        return {"outcome": "refused", "id": lead_id,
                "reason": f"lead {lead_id} does not exist"}
    return {"outcome": "answered", "id": lead_id, "lead": lead}
