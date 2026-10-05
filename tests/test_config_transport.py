"""config.load_env and transport.mcp_client.client_from_env. Offline: _login is stubbed."""
from __future__ import annotations

import pytest

import config
from transport import mcp_client
from transport.mcp_client import MCPClient, client_from_env

KEYS = ("T6TEST_FOO", "T6TEST_BAR", "T6TEST_QUOTED", "T6TEST_FIRST", "T6TEST_SHARED",
        "T6TEST_REPO_ONLY")


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    """(agent .env path, repo .env path), with the real environment cleared of our keys."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    agent_env, repo_env = tmp_path / "agent.env", tmp_path / "repo.env"
    monkeypatch.setattr(config, "REPO_ENV_PATH", repo_env)
    return agent_env, repo_env


@pytest.fixture
def no_network(monkeypatch):
    def refuse(self):
        raise AssertionError("tried to log in over the network")
    monkeypatch.setattr(MCPClient, "_login", refuse)


# --- load_env -----------------------------------------------------------------

def test_export_prefix_quotes_and_comments(env_files):
    agent_env, _ = env_files
    agent_env.write_text("# a comment\n"
                         "export T6TEST_FOO=bar\n"
                         "T6TEST_QUOTED=\"hello world\"\n"
                         "T6TEST_BAR = 'single'\n"
                         "#T6TEST_SHARED=commented-out\n"
                         "not a key value line\n", encoding="utf-8")
    env = config.load_env(agent_env)
    assert env["T6TEST_FOO"] == "bar"
    assert env["T6TEST_QUOTED"] == "hello world"
    assert env["T6TEST_BAR"] == "single"
    assert "T6TEST_SHARED" not in env


def test_utf8_bom_does_not_break_the_first_key(env_files):
    agent_env, _ = env_files
    agent_env.write_text("T6TEST_FIRST=one\n", encoding="utf-8-sig")
    assert config.load_env(agent_env)["T6TEST_FIRST"] == "one"


def test_precedence_process_then_agent_then_repo(env_files, monkeypatch):
    agent_env, repo_env = env_files
    agent_env.write_text("T6TEST_SHARED=agent\nT6TEST_FOO=agent\n", encoding="utf-8")
    repo_env.write_text("T6TEST_SHARED=repo\nT6TEST_FOO=repo\nT6TEST_REPO_ONLY=repo\n",
                        encoding="utf-8")
    monkeypatch.setenv("T6TEST_SHARED", "process")

    env = config.load_env(agent_env)

    assert env["T6TEST_SHARED"] == "process"
    assert env["T6TEST_FOO"] == "agent"
    assert env["T6TEST_REPO_ONLY"] == "repo"


def test_missing_files_are_fine(env_files):
    agent_env, _ = env_files
    env = config.load_env(agent_env)
    assert not any(k in env for k in KEYS)


# --- client_from_env ----------------------------------------------------------

def test_token_only_client_never_logs_in(no_network):
    client = client_from_env({"TOKEN": "tok-123"})
    assert client.token == "tok-123"
    assert client.base_url == mcp_client.SURYODAYA


def test_base_url_override(no_network):
    client = client_from_env({"TOKEN": "t", "AS": "http://localhost:9000/"})
    assert client.base_url == "http://localhost:9000"


def test_no_credentials_raises():
    with pytest.raises(RuntimeError, match=r"EMAIL \+ SURYODAYA_PW"):
        client_from_env({})


def test_password_is_preferred_over_token(monkeypatch):
    monkeypatch.setattr(MCPClient, "_login", lambda self: "from-login")
    client = client_from_env({"EMAIL": "me@example.com", "SURYODAYA_PW": "pw", "TOKEN": "tok"})
    assert client.token == "from-login"
