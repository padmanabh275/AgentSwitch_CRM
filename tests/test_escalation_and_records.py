"""domain/escalation.py and domain/records.py."""
from __future__ import annotations

import pytest

from domain import escalation, records
from transport.mcp_client import FORBIDDEN, NOT_FOUND, MCPToolError

SUBJECT = escalation.quote_subject("item-1", 500)


def esc_list(*rows):
    return lambda args: {"data": list(rows)}


def test_quote_subject_is_stable_and_prefixed():
    assert SUBJECT == "T6-BOM quote for 500x item-1"
    assert escalation.quote_subject("item-1", 500) == SUBJECT


# --- find_open --------------------------------------------------------------

@pytest.mark.parametrize("status", escalation.TERMINAL_STATUSES)
def test_find_open_ignores_terminal_escalations(fake_client, status):
    client = fake_client({"AgentEscalation.list": esc_list({"id": "e1", "status": status})})
    assert escalation.find_open(client, SUBJECT) is None
    assert client.calls[0][1] == {"subject": SUBJECT, "limit": 20}


def test_find_open_returns_first_standing_escalation(fake_client):
    client = fake_client({"AgentEscalation.list": esc_list(
        {"id": "old", "status": "withdrawn"},
        {"id": "e2", "status": "open"},
        {"id": "e3", "status": "open"})})
    assert escalation.find_open(client, SUBJECT)["id"] == "e2"


def test_find_open_treats_unfamiliar_status_as_standing(fake_client):
    client = fake_client({"AgentEscalation.list": esc_list({"id": "e1", "status": "snoozed"})})
    assert escalation.find_open(client, SUBJECT)["id"] == "e1"


# --- file_escalation --------------------------------------------------------

def test_file_escalation_reuses_an_open_one(fake_client):
    client = fake_client({"AgentEscalation.list": esc_list(
        {"id": "e1", "number": "ESC-1", "status": "open", "assignee_display": "Meera"})})

    out = escalation.file_escalation(client, "s1", "why", subject=SUBJECT)

    assert client.tools_called() == ["AgentEscalation.list"]
    assert out == {"outcome": "escalated", "escalation_id": "e1", "number": "ESC-1",
                   "status": "open", "assignee": "Meera", "reused_existing": True}


def test_file_escalation_creates_with_prefixed_subject(fake_client):
    created = {"id": "e9", "number": "ESC-9", "status": "open", "assignee_display": None}
    client = fake_client({"AgentEscalation.list": esc_list(),
                          "AgentEscalation.create": lambda args: created})

    out = escalation.file_escalation(client, "s1", "why", reason_code="data_issue",
                                     subject="needs a human", party_id="p1")

    tool, args = client.calls[-1]
    assert tool == "AgentEscalation.create"
    assert args == {"session_id": "s1", "reason": "why", "reason_code": "data_issue",
                    "subject": "T6-needs a human", "party_id": "p1"}
    assert client.calls[0][1]["subject"] == "T6-needs a human"  # duplicate check uses the same key
    assert out["reused_existing"] is False
    assert out["assignee"] is None  # read back from the record, never assumed


def test_file_escalation_defaults(fake_client):
    client = fake_client({"AgentEscalation.list": esc_list(),
                          "AgentEscalation.create": lambda args: {"id": "e1"}})
    escalation.file_escalation(client, None, "why")
    args = client.calls[-1][1]
    assert args["subject"] == "T6-escalation"
    assert args["reason_code"] == "other"
    assert "party_id" not in args


def test_file_escalation_dry_run_reads_but_never_creates(fake_client):
    client = fake_client({"AgentEscalation.list": esc_list()})

    out = escalation.file_escalation(client, "s1", "why", subject=SUBJECT, dry_run=True)

    assert client.tools_called() == ["AgentEscalation.list"]
    assert out["dry_run"] is True
    assert out["escalation_id"] is None
    assert out["would_file"]["subject"] == SUBJECT


# --- records ----------------------------------------------------------------

@pytest.mark.parametrize("fn, entity, key", [
    (records.get_deal, "Deal", "deal"),
    (records.get_lead, "Lead", "lead"),
])
def test_get_found(fake_client, fn, entity, key):
    client = fake_client({f"{entity}.get": lambda args: {"id": args["id"], "title": "x"}})
    out = fn(client, "r1")
    assert out == {"outcome": "answered", "id": "r1", key: {"id": "r1", "title": "x"}}


@pytest.mark.parametrize("fn, entity, key", [
    (records.get_deal, "Deal", "deal"),
    (records.get_lead, "Lead", "lead"),
])
def test_get_not_found_is_a_refusal(fake_client, fn, entity, key):
    client = fake_client({f"{entity}.get": [MCPToolError("nope", code=NOT_FOUND)]})
    out = fn(client, "r1")
    assert out == {"outcome": "refused", "id": "r1", "reason": f"{key} r1 does not exist"}


def test_get_other_errors_are_not_disguised_as_missing(fake_client):
    client = fake_client({"Deal.get": [MCPToolError("no", code=FORBIDDEN)]})
    with pytest.raises(MCPToolError) as e:
        records.get_deal(client, "r1")
    assert e.value.code == FORBIDDEN
