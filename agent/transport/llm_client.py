"""call_llm(): the agent's only path to a model. Two backends, one contract:

- OpenAI-compatible (`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`):
  what the hosted AgentSwitch harness run provides — the platform's model,
  tool calls supported. Used whenever OPENAI_BASE_URL or OPENAI_API_KEY is set.
- glc_v5 (the user's own local LLM gateway — a separate repo, not part of
  AgentSwitch): the default for local runs. `uv run glc serve`, port 8111.
  See docs/SALES_AGENT_DESIGN.md → "Architecture".

Either way callers see glc_v5's shapes: messages whose assistant turns carry
tool_calls as [{"id","name","arguments": dict}], tools as
[{"name","description","input_schema"}], and a reply dict with text,
tool_calls, stop_reason ("max_tokens" when cut off) and parsed. The OpenAI
translation lives entirely in this file.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

GLC_URL = os.environ.get("GLC_URL", "http://127.0.0.1:8111")
# Un-pinned, glc_v5's default candidate order tries `ollama` first — a local
# model whose tool_call_dialect is "prompted_fallback" (asked to emit tool
# calls as text, then parsed back out). In practice that parse doesn't
# recover structured tool_calls reliably (observed live: gemma3:12b emitted
# two newline-separated {"tool_call": ...} objects and got 0 back). `gemini`
# is the provider actually configured with a live key here and reports
# native `"tools": true`, so it's the default; override via GLC_PROVIDER if
# your gateway config differs.
GLC_PROVIDER = os.environ.get("GLC_PROVIDER", "gemini")

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"   # what the openai SDK assumes
DEFAULT_OPENAI_MODEL = "agentswitch-default"             # the platform example's fallback
TRANSIENT_RETRIES = 2
_FINISH = {"length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use",
           "stop": "end_turn", "content_filter": "refusal"}


class LLMError(Exception):
    pass


def openai_config(env: dict | None = None) -> dict | None:
    """The OpenAI-compatible backend's settings, or None to use glc_v5."""
    env = os.environ if env is None else env
    if not (env.get("OPENAI_BASE_URL") or env.get("OPENAI_API_KEY")):
        return None
    return {"base_url": (env.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).rstrip("/"),
            "api_key": env.get("OPENAI_API_KEY", ""),
            "model": env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL}


def backend_url() -> str:
    """Where model calls go — for the harness's up-check and its logs."""
    cfg = openai_config()
    return cfg["base_url"] if cfg else GLC_URL


def call_llm(messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto", max_tokens: int = 1024,
             temperature: float = 0.0, provider: str | None = GLC_PROVIDER,
             response_schema: dict | None = None) -> dict:
    """Returns the reply dict (text, tool_calls, stop_reason, parsed, ...).
    tools is a list of {"name","description","input_schema"}.

    response_schema asks for JSON matching that schema. The object comes back
    in `parsed` — callers must still validate it. `provider` only applies to
    glc_v5.
    """
    cfg = openai_config()
    if cfg:
        return _call_openai(cfg, messages, tools, tool_choice, max_tokens,
                            temperature, response_schema)
    return _call_glc(messages, tools, tool_choice, max_tokens, temperature,
                     provider, response_schema)


# --- glc_v5 -------------------------------------------------------------------

def _call_glc(messages, tools, tool_choice, max_tokens, temperature, provider,
              response_schema) -> dict:
    """POST /v1/chat on glc_v5."""
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "agent": "sales_pipeline_seat6",
    }
    if provider:
        payload["provider"] = provider
    if response_schema:
        payload["response_format"] = {"type": "json_schema", "schema": response_schema}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        GLC_URL + "/v1/chat", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        raw = urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        raise LLMError(f"glc_v5 HTTP {e.code}: {e.read()[:500]!r}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise LLMError(f"glc_v5 unreachable at {GLC_URL} — is `uv run glc serve` running? ({e})")
    try:
        return json.loads(raw)
    except ValueError:
        raise LLMError(f"glc_v5 returned non-JSON: {raw[:200]!r}")


# --- OpenAI-compatible --------------------------------------------------------

class _BadRequest(LLMError):
    pass


def _call_openai(cfg, messages, tools, tool_choice, max_tokens, temperature,
                 response_schema) -> dict:
    payload = {"model": cfg["model"], "messages": to_openai_messages(messages),
               "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        payload["tools"] = to_openai_tools(tools)
        payload["tool_choice"] = tool_choice
    if response_schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "response", "schema": response_schema, "strict": False}}
    try:
        data = _post_openai(cfg, payload)
    except _BadRequest:
        # Not every OpenAI-compatible server takes json_schema or a
        # temperature; ask once more without them, with the schema in the
        # prompt so the reply is still JSON.
        if not (response_schema or "temperature" in payload):
            raise
        payload.pop("temperature", None)
        if payload.pop("response_format", None):
            payload["messages"] = _with_schema_instruction(payload["messages"], response_schema)
        data = _post_openai(cfg, payload)
    return from_openai_response(data, want_json=bool(response_schema))


def _post_openai(cfg: dict, payload: dict) -> dict:
    url = cfg["base_url"] + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    body = json.dumps(payload).encode()
    for attempt in range(TRANSIENT_RETRIES + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            raw = urllib.request.urlopen(req, timeout=120).read()
            break
        except urllib.error.HTTPError as e:
            detail = e.read()[:500]
            if e.code == 400:
                raise _BadRequest(f"model HTTP 400: {detail!r}")
            if (e.code == 429 or e.code >= 500) and attempt < TRANSIENT_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError(f"model HTTP {e.code}: {detail!r}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < TRANSIENT_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError(f"model unreachable at {cfg['base_url']} ({e})")
    try:
        return json.loads(raw)
    except ValueError:
        raise LLMError(f"model returned non-JSON: {raw[:200]!r}")


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
        for t in tools]


def to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": [{"id": tc["id"], "type": "function", "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments") or {})}}
                            for tc in m["tool_calls"]]})
        elif m.get("role") == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                        "content": m.get("content", "")})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def from_openai_response(data: dict, want_json: bool = False) -> dict:
    try:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"model reply had no choices: {json.dumps(data)[:300]}")
    text = msg.get("content") or ""
    tool_calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
        tool_calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""),
                           "arguments": args})
    finish = choice.get("finish_reason")
    reply = {"text": text, "tool_calls": tool_calls,
             "stop_reason": _FINISH.get(finish, finish), "model": data.get("model"),
             "usage": data.get("usage"), "parsed": None}
    if want_json:
        reply["parsed"] = _parse_json_object(text)
    return reply


def _parse_json_object(text: str) -> dict | None:
    """The JSON object in a reply, tolerating a ```json fence around it."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text.strip()
    try:
        obj = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(candidate[start:end + 1])
        except ValueError:
            return None
    return obj if isinstance(obj, dict) else None


def _with_schema_instruction(messages: list[dict], schema: dict) -> list[dict]:
    note = ("Reply with only a JSON object (no prose, no code fence) matching this "
            "JSON Schema:\n" + json.dumps(schema))
    if messages and messages[0].get("role") == "system":
        return [{**messages[0], "content": messages[0]["content"] + "\n\n" + note}, *messages[1:]]
    return [{"role": "system", "content": note}, *messages]
