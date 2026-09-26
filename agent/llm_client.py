"""call_llm(): the agent's only path to a model, via glc_v5 (the user's own
local LLM gateway — a separate repo, not part of AgentSwitch).

glc_v5 must be running (`uv run glc serve`, port 8111 by default) alongside
this agent. See SALES_AGENT_DESIGN.md's "Architecture locked" section.
"""
from __future__ import annotations

import json
import os
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


class LLMError(Exception):
    pass


def call_llm(messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto", max_tokens: int = 1024,
             temperature: float = 0.0, provider: str | None = GLC_PROVIDER) -> dict:
    """POST /v1/chat. Returns the parsed ChatResponse dict (text, tool_calls,
    stop_reason, ...). tools is a list of {"name","description","input_schema"}.
    """
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "agent": "sales_pipeline_seat6",
    }
    if provider:
        payload["provider"] = provider
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
    except urllib.error.URLError as e:
        raise LLMError(f"glc_v5 unreachable at {GLC_URL} — is `uv run glc serve` running? ({e})")
    return json.loads(raw)
