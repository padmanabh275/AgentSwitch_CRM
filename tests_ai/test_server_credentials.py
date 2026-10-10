"""AI-WRITTEN (Claude Opus 5.5). Regression scaffolding only, not a graded hand-written test.

transport.mcp_client.client_from_env on the hosted harness run: AGENTSWITCH_TOKEN
and AGENTSWITCH_BASE_URL must win over any local credentials, and never log in.
"""
from __future__ import annotations

import pytest

from transport import mcp_client
from transport.mcp_client import MCPClient, client_from_env, has_credentials


@pytest.fixture
def no_network(monkeypatch):
    def refuse(self):
        raise AssertionError("tried to log in over the network")
    monkeypatch.setattr(MCPClient, "_login", refuse)


def test_server_token_and_base_url_are_used(no_network):
    client = client_from_env({"AGENTSWITCH_TOKEN": "seat-tok",
                              "AGENTSWITCH_BASE_URL": "https://copy.example/"})
    assert client.token == "seat-tok"
    assert client.base_url == "https://copy.example"


def test_server_token_beats_local_password_and_url(no_network):
    client = client_from_env({"AGENTSWITCH_TOKEN": "seat-tok",
                              "AGENTSWITCH_BASE_URL": "https://copy.example",
                              "EMAIL": "me@example.com", "SURYODAYA_PW": "pw",
                              "TOKEN": "local-tok", "AS": "http://localhost:9000"})
    assert client.token == "seat-tok"
    assert client.base_url == "https://copy.example"


def test_server_token_without_base_url_defaults_to_suryodaya(no_network):
    client = client_from_env({"AGENTSWITCH_TOKEN": "seat-tok"})
    assert client.base_url == mcp_client.SURYODAYA


def test_server_token_client_never_relogs_on_auth(no_network):
    # Token-only: a 401 must surface as `auth`, not trigger a password login.
    client = client_from_env({"AGENTSWITCH_TOKEN": "seat-tok"})
    assert client._password == ""


@pytest.mark.parametrize("env,expected", [
    ({"AGENTSWITCH_TOKEN": "t"}, True),
    ({"SURYODAYA_PW": "pw"}, True),
    ({"TOKEN": "t"}, True),
    ({"AGENTSWITCH_BASE_URL": "https://x"}, False),
    ({}, False),
])
def test_has_credentials(env, expected):
    assert has_credentials(env) is expected
