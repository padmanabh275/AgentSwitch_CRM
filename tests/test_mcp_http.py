"""transport/mcp_client.py over HTTP: urlopen is scripted, so nothing leaves the machine."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from transport import mcp_client
from transport.mcp_client import (AUTH, FORBIDDEN, INVALID_ARGUMENTS, NOT_FOUND, TRANSIENT,
                                  UNKNOWN, MCPClient, MCPToolError)


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body


def http_error(code, body=b""):
    return urllib.error.HTTPError("http://test", code, "err", {}, io.BytesIO(body))


class Server:
    """Each scripted entry is a dict (JSON body), raw bytes, or an exception to raise."""

    def __init__(self):
        self.script: list = []
        self.requests: list[dict] = []

    def __call__(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "auth": req.get_header("Authorization"),
                              "body": json.loads(req.data)})
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return Response(reply if isinstance(reply, bytes) else json.dumps(reply).encode())


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", s)
    return s


def client(token="tok", password=""):
    return MCPClient("me@example.com", password, base_url="http://test/", token=token)


def ok(structured):
    return {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": structured}}


def error_code(server, reply, c=None):
    server.script.append(reply)
    with pytest.raises(MCPToolError) as e:
        (c or client()).call("Deal.get", {"id": "d1"})
    return e.value


# --- success ------------------------------------------------------------------

def test_call_posts_json_rpc_and_returns_structured_content(server):
    server.script.append(ok({"id": "d1"}))
    assert client().call("Deal.get", {"id": "d1"}) == {"id": "d1"}

    req = server.requests[0]
    assert req["url"] == "http://test/api/mcp"
    assert req["auth"] == "Bearer tok"
    assert req["body"]["method"] == "tools/call"
    assert req["body"]["params"] == {"name": "Deal.get", "arguments": {"id": "d1"}}


def test_missing_arguments_are_sent_as_empty(server):
    server.script.append(ok({}))
    client().call("Deal.list")
    assert server.requests[0]["body"]["params"]["arguments"] == {}


def test_list_tools_follows_the_cursor(server):
    server.script += [{"result": {"tools": [{"name": "A"}], "nextCursor": "c2"}},
                      {"result": {"tools": [{"name": "B"}]}}]
    assert [t["name"] for t in client().list_tools()] == ["A", "B"]
    assert server.requests[1]["body"]["params"] == {"cursor": "c2"}


# --- error mapping ------------------------------------------------------------

def test_platform_error_code_is_kept(server):
    e = error_code(server, {"error": {"message": "Deal not found.", "data": {"code": "not_found"}}})
    assert e.code == NOT_FOUND
    assert "Deal not found." in str(e)


def test_invalid_arguments_carries_field_errors(server):
    errors = [{"field": "/id", "message": "must be a uuid"}]
    e = error_code(server, {"error": {"message": "bad", "data": {"code": "invalid_arguments",
                                                                 "errors": errors}}})
    assert e.code == INVALID_ARGUMENTS and e.errors == errors


def test_error_without_a_code_is_unknown(server):
    assert error_code(server, {"error": {"message": "huh"}}).code == UNKNOWN


def test_is_error_result_is_unknown(server):
    assert error_code(server, {"result": {"isError": True, "content": []}}).code == UNKNOWN


@pytest.mark.parametrize("reply, code", [
    (http_error(401), AUTH),
    (http_error(403, b"App 'x' is not enabled"), FORBIDDEN),
    (http_error(429), TRANSIENT),
    (http_error(500), TRANSIENT),
    (http_error(503), TRANSIENT),
    (urllib.error.URLError("dns"), TRANSIENT),
    (TimeoutError("slow"), TRANSIENT),
    (ConnectionError("reset"), TRANSIENT),
    (b"<html>gateway</html>", TRANSIENT),
])
def test_http_failures_are_mapped(server, reply, code):
    assert error_code(server, reply).code == code


def test_other_4xx_still_reads_the_envelope(server):
    body = json.dumps({"error": {"message": "x", "data": {"code": "invalid_transition"}}}).encode()
    assert error_code(server, http_error(409, body)).code == "invalid_transition"


# --- re-login -----------------------------------------------------------------

def test_auth_failure_logs_in_again_once(server):
    server.script += [http_error(401), {"token": "new"}, ok({"ok": True})]

    assert client(token="old", password="pw").call("Deal.list", {}) == {"ok": True}

    assert server.requests[1]["url"] == "http://test/api/auth/login"
    assert server.requests[1]["body"] == {"email": "me@example.com", "password": "pw"}
    assert server.requests[2]["auth"] == "Bearer new"


def test_second_auth_failure_is_raised(server):
    server.script += [http_error(401), {"token": "new"}]
    e = error_code(server, http_error(401), c=client(token="old", password="pw"))
    assert e.code == AUTH
    assert len(server.requests) == 3


def test_token_only_client_does_not_log_in(server):
    assert error_code(server, http_error(401)).code == AUTH
    assert len(server.requests) == 1


# --- login --------------------------------------------------------------------

@pytest.mark.parametrize("reply, message", [
    (http_error(401, b"bad password"), "login failed: HTTP 401"),
    (urllib.error.URLError("dns"), "unreachable"),
    ({"user": "me"}, "login response had no token"),
    (b"not json", "login response had no token"),
])
def test_login_failures_raise_runtime_error(server, reply, message):
    server.script.append(reply)
    with pytest.raises(RuntimeError, match=message):
        MCPClient("me@example.com", "pw", base_url="http://test")


def test_login_sets_the_token(server):
    server.script.append({"token": "fresh"})
    assert MCPClient("me@example.com", "pw", base_url="http://test").token == "fresh"
