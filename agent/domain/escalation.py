"""Escalation filing (AgentEscalation.create). Zero LLM."""
from __future__ import annotations

from transport.mcp_client import MCPClient

T6_PREFIX = "T6-"

# Statuses after which an escalation no longer stands. Only "open" and
# "withdrawn" have been observed live (2026-09-26); anything not listed
# here is treated as still open, so an unfamiliar status errs toward
# reusing the existing escalation rather than paging a human twice.
TERMINAL_STATUSES = ("withdrawn", "resolved", "closed", "cancelled", "rejected")


def quote_subject(item_id: str, qty: int) -> str:
    """Stable subject for a quote escalation — the idempotency key."""
    return f"{T6_PREFIX}BOM quote for {qty}x {item_id}"


def _summary(record: dict, reused: bool) -> dict:
    """What we report about an escalation. Assignee comes from the record
    itself — our own ESC-2026-00026 came back with assignee_user_id null,
    so claiming "sent to Meera" without reading it back would be invented.
    """
    return {
        "outcome": "escalated",
        "escalation_id": record.get("id"),
        "number": record.get("number"),
        "status": record.get("status"),
        "assignee": record.get("assignee_display"),
        "reused_existing": reused,
    }


def find_open(client: MCPClient, subject: str) -> dict | None:
    """An escalation with this exact subject that still stands, if any.
    The subject filter on AgentEscalation.list is exact-match (verified live)."""
    page = client.call("AgentEscalation.list", {"subject": subject, "limit": 20})
    for row in page.get("data") or []:
        if row.get("status") not in TERMINAL_STATUSES:
            return row
    return None


def file_escalation(client: MCPClient, session_id: str | None, reason: str,
                     reason_code: str = "other", subject: str | None = None,
                     party_id: str | None = None, dry_run: bool = False) -> dict:
    """Wraps AgentEscalation.create. reason_code defaults to "other" because
    the enum has no value for a cross-app capability gap (bug candidate E1) —
    the single most predictable escalation reason on a seat-scoped platform.

    Idempotent per subject: if an escalation with the same subject is still
    open, it's returned instead of filing another one. Every run otherwise
    pages the same human again for the same question.

    dry_run: reads (the duplicate check) still happen; the create doesn't.
    """
    subject = subject or f"{T6_PREFIX}escalation"
    if not subject.startswith(T6_PREFIX):
        subject = T6_PREFIX + subject

    existing = find_open(client, subject)
    if existing:
        return _summary(existing, reused=True)

    args = {"session_id": session_id, "reason": reason, "reason_code": reason_code,
            "subject": subject}
    if party_id:
        args["party_id"] = party_id
    if dry_run:
        return {"outcome": "escalated", "escalation_id": None, "dry_run": True,
                "would_file": args}
    return _summary(client.call("AgentEscalation.create", args), reused=False)
