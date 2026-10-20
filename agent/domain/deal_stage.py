"""Move an open deal forward one stage. Zero LLM.

The funnel is new -> qualification -> proposal -> negotiation, one
transition tool per step (live 2026-10-20; Deal.update can't set the
stage). The instructor removed the stage-skipping shortcuts on purpose
(reply to N191), so this agent takes exactly one step per request:

- the deal must exist and be open; a name must match exactly one deal;
- the step is the deal's next stage. A target further on is refused (no
  skipping), as is one behind (no going back) or the current stage;
- from negotiation the next step is won or lost: a human's call, refused;
- the read right before the write is the one the step is chosen from, and
  the deal is read back after it. If the platform says the transition no
  longer applies (someone else moved the deal), that is a refusal.

The write is never retried (registry: retries=0).
"""
from __future__ import annotations

import re

from domain.deals import OPEN_STAGES
from transport.mcp_client import INVALID_TRANSITION, NOT_FOUND, MCPClient, MCPToolError

# stage -> (next stage, the transition tool that makes that move)
NEXT_STEP = {
    "new": ("qualification", "Deal.qualify"),
    "qualification": ("proposal", "Deal.send_proposal"),
    "proposal": ("negotiation", "Deal.negotiate"),
}
ADVANCE_STAGES = tuple(stage for stage, _ in NEXT_STEP.values())
DEAL_FIELDS = ("id", "title", "stage", "updated_at")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _pick(row: dict) -> dict:
    return {k: row.get(k) for k in DEAL_FIELDS}


def find_deal(client: MCPClient, ref: str) -> tuple[dict | None, str | None]:
    """(deal, None) or (None, why not). By id, else by exact title. The title
    filter is checked here too: a filter the server ignored would look like
    a match."""
    ref = (ref or "").strip()
    if UUID_RE.fullmatch(ref):
        try:
            return _pick(client.call("Deal.get", {"id": ref})), None
        except MCPToolError as e:
            if e.code != NOT_FOUND:
                raise
            return None, f"deal {ref} does not exist"
    rows = client.call("Deal.list", {"title": ref, "limit": 50}).get("data") or []
    same = [r for r in rows if (r.get("title") or "").strip().casefold() == ref.casefold()]
    if not same:
        return None, f"no deal is titled {ref!r}"
    if len(same) > 1:
        return None, f"{len(same)} deals are titled {ref!r}; give the deal id"
    return _pick(same[0]), None


def plan_step(stage: str | None, target: str | None) -> tuple[tuple[str, str] | None, str | None]:
    """((next stage, tool), None) for the one allowed step, or (None, why not)."""
    if stage not in OPEN_STAGES:
        return None, f"it is {stage}, not open"
    if stage not in NEXT_STEP:
        return None, (f"it is at {stage}; the next step is marking it won or lost, "
                      "which is a person's decision, not this agent's")
    nxt, tool = NEXT_STEP[stage]
    if target in (None, nxt):
        return (nxt, tool), None
    if target == stage:
        return None, f"it is already at {stage}"
    order = list(OPEN_STAGES)
    if target in order and order.index(target) < order.index(stage):
        return None, f"it is at {stage}; this agent only moves deals forward, not back to {target}"
    return None, (f"it is at {stage}, so the next stage is {nxt}; moving straight to "
                  f"{target} would skip a stage")


def advance_deal(client: MCPClient, deal_ref: str, target: str | None = None,
                 dry_run: bool = False) -> dict:
    """outcome: advanced (moved and read back), would_advance (dry run),
    refused (see the module doc), error (the move didn't read back)."""
    base = {"deal_ref": deal_ref, "target_stage": target}
    if not (deal_ref or "").strip():
        return {**base, "outcome": "refused", "reason": "can't move a deal without knowing which one"}
    deal, why = find_deal(client, deal_ref)
    if deal is None:
        return {**base, "outcome": "refused", "reason": why}
    base.update(deal_id=deal["id"], deal_title=deal["title"], stage_before=deal["stage"],
                updated_at_before=deal["updated_at"])
    step, why = plan_step(deal["stage"], target)
    if step is None:
        return {**base, "outcome": "refused",
                "reason": f"deal {deal['title']!r} was not moved: {why}"}
    nxt, tool = step
    base.update(next_stage=nxt, tool=tool)
    if tool not in {t.get("name") for t in client.list_tools()}:
        return {**base, "outcome": "refused",
                "reason": f"deal {deal['title']!r} was not moved: no {tool} in this seat's catalogue"}
    if dry_run:
        return {**base, "outcome": "would_advance", "dry_run": True,
                "reason": f"dry run: deal {deal['title']!r} would move {deal['stage']} -> {nxt} "
                          f"with {tool}; nothing was written"}
    try:
        client.call(tool, {"id": deal["id"]})
    except MCPToolError as e:
        if e.code != INVALID_TRANSITION:
            raise
        now = _pick(client.call("Deal.get", {"id": deal["id"]}))
        return {**base, "outcome": "refused", "stage_now": now["stage"],
                "reason": f"deal {deal['title']!r} was {deal['stage']} when read but is "
                          f"{now['stage']} now: it changed underneath this run, so it was not moved"}
    after = _pick(client.call("Deal.get", {"id": deal["id"]}))
    if after["stage"] != nxt:
        return {**base, "outcome": "error", "stage_after": after["stage"],
                "reason": f"{tool} returned, but deal {deal['title']!r} reads back as {after['stage']!r}"}
    return {**base, "outcome": "advanced", "stage_after": after["stage"],
            "updated_at_after": after["updated_at"],
            "reason": f"moved deal {deal['title']!r} {deal['stage']} -> {nxt} with {tool}; "
                      f"read back as {nxt}"}
