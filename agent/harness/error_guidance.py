"""Do wrong-state errors name only steps this seat can take? (Release 13, N484)

A refused transition says where the record can go next, e.g.

    No transition from 'draft' to 'declined' in flow 'QuotationFlow': ...
    From 'draft' you can go to: pending_approval (Submit for Margin Approval); sent (Send)

An agent that follows that guidance calls the tool for one of those steps,
so every step named must have a tool in this seat's tools/list. Before
Release 13 the Quotation errors offered Cancel and Expire, which the seat
has no tool for (drafts/BUG9).

The sweep reads our own records (created_by the seat) and, for one record
per open status of each entity, calls a closing transition from a
*different* status, which the platform must refuse. The status is read
again just before the call, and only closing edges are used (decline,
mark_lost, disqualify), so even a call that wrongly landed would only close
our own test record. Nothing else is written.

Usage (from agent/):
    uv run python -m harness.error_guidance            # Suryodaya, seat from agent/.env
Exit code 1 if a step has no tool, or a probe call was not refused.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

from transport.mcp_client import INVALID_TRANSITION, MCPToolError

# entity -> (the field holding its state, the closing verb used to probe it)
ENTITIES = {"Quotation": ("status", "decline"),
            "Deal": ("stage", "mark_lost"),
            "Lead": ("status", "disqualify")}
TERMINAL = {"Quotation": ("converted", "declined", "expired", "cancelled"),
            "Deal": ("closed_won", "closed_lost"),
            "Lead": ("converted", "disqualified")}
NEXT_RE = re.compile(r"From '([^']+)' you can go to: (.+?)\s*$", re.S)
STEP_RE = re.compile(r"([\w-]+) \(([^)]+)\)")


def parse_next_steps(message: str) -> tuple[str | None, list[tuple[str, str]] | None]:
    """(from status, [(target, action), ...]) from a refusal message.
    Terminal ("nowhere") gives []; a message without guidance gives (None, None)."""
    m = NEXT_RE.search(message or "")
    if not m:
        return None, None
    rest = m.group(2)
    if rest.startswith("nowhere"):
        return m.group(1), []
    return m.group(1), STEP_RE.findall(rest)


def verb(action: str) -> str:
    """'Submit for Margin Approval' -> 'submit_for_margin_approval'."""
    return re.sub(r"[^a-z0-9]+", "_", action.lower()).strip("_")


def tool_for_step(entity: str, source: str, target: str, action: str, names: set[str]) -> str | None:
    """The seat's tool for that step: Entity.verb.from.to, else a bare Entity.verb
    (a transition tool that takes its edge from the record, like Quotation.send)."""
    for name in (f"{entity}.{verb(action)}.{source}.{target}", f"{entity}.{verb(action)}"):
        if name in names:
            return name
    return None


def missing_steps(entity: str, source: str, steps: list[tuple[str, str]], names: set[str]) -> list[str]:
    return [f"{target} ({action})" for target, action in steps
            if not tool_for_step(entity, source, target, action, names)]


def probe_tool(entity: str, status: str, names: set[str]) -> str | None:
    """A closing transition whose edge starts somewhere other than `status`,
    so the platform must refuse it on a record in `status`."""
    closing = ENTITIES[entity][1]
    for name in sorted(names):
        parts = name.split(".")
        if len(parts) == 4 and parts[0] == entity and parts[1] == closing and parts[2] != status:
            return name
    return None


def sweep(client, me: str) -> list[dict]:
    """One probe per open status of our own records, per entity."""
    names = {t.get("name") for t in client.list_tools()}
    out = []
    for entity, (field, _) in ENTITIES.items():
        rows = client.call(f"{entity}.list", {"limit": 1000}).get("data") or []
        seen: set[str] = set()
        for row in rows:
            status = row.get(field)
            if row.get("created_by") != me or status in TERMINAL[entity] or status in seen:
                continue
            seen.add(status)
            out.append(_probe(client, entity, field, row, status, names))
    return out


def _probe(client, entity: str, field: str, row: dict, status: str, names: set[str]) -> dict:
    res = {"entity": entity, "id": row["id"], "status": status}
    tool = probe_tool(entity, status, names)
    if not tool:
        return {**res, "result": "skipped", "why": "no closing transition from another status"}
    fresh = client.call(f"{entity}.get", {"id": row["id"]})
    fresh = fresh.get("result", fresh) if "id" not in fresh else fresh
    if fresh.get(field) != status or tool.split(".")[2] == fresh.get(field):
        return {**res, "tool": tool, "result": "skipped", "why": f"status moved to {fresh.get(field)}"}
    try:
        client.call(tool, {"id": row["id"]})
    except MCPToolError as e:
        source, steps = parse_next_steps(str(e))
        res.update(tool=tool, code=e.code, message=str(e)[:400], source=source,
                   steps=[f"{t} ({a})" for t, a in steps or []])
        if e.code != INVALID_TRANSITION:
            return {**res, "result": "fail", "why": f"refused with code {e.code}, not {INVALID_TRANSITION}"}
        if steps is None:
            return {**res, "result": "fail", "why": "the error names no next steps"}
        missing = missing_steps(entity, source, steps, names)
        return {**res, "missing": missing, "result": "fail" if missing else "pass",
                "why": f"no tool for {', '.join(missing)}" if missing else "every step has a tool"}
    return {**res, "tool": tool, "result": "fail", "why": f"{tool} was NOT refused: it landed on our record"}


def main() -> int:
    from config import RUNS_DIR, load_env
    from transport.mcp_client import client_from_env
    client = client_from_env(load_env())
    rows = client.call("Lead.list", {"search": "T6-harness", "limit": 1}).get("data") or []
    me = rows[0].get("created_by") if rows else None
    if not me:
        print("no T6-harness lead to tell which records are ours; nothing probed")
        return 1
    results = sweep(client, me)
    for r in results:
        print(f"{r['result']:8} {r['entity']:9} {r['status']:15} {r.get('tool') or '-':45} "
              f"{' ; '.join(r.get('steps') or []) or '-'}  [{r['why']}]")
    path = RUNS_DIR / f"error_guidance-{dt.datetime.now():%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"[saved {path}]")
    return 1 if any(r["result"] == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
