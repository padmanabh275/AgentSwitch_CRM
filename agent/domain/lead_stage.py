"""Move a lead forward one status, converting it into a deal at the end. Zero LLM.

The funnel is new -> contacted -> qualified -> converted, one transition
tool per step (live 2026-10-21). The instructor removed the shortcuts that
skipped steps (reply to N191), so this agent takes exactly one step per
request, as domain/deal_stage.py does for deals:

- the lead must exist and be open (new, contacted or qualified);
- the step is the lead's next status. A target further on is refused (no
  skipping), as is one behind or the current status;
- converted and disqualified leads are closed: nothing to move.

Converting uses Lead.convert, not Lead.make.Deal. Probed live 2026-10-21:
Lead.convert marks the lead converted *and* creates its deal ("Deal from
<party>"); Lead.make.Deal creates a deal but leaves the lead qualified.
Until Release 13 a later Lead.convert then created a second deal for the
same lead (drafts/BUG8); since Release 13 (N482) it links the deal the lead
already has. So before converting, the lead's existing deals are read
(Deal.list by lead_id, checked here too): with one, the convert must link
it; with more than one, which to link is ambiguous and the convert is
refused. After converting, the lead is read back as converted with exactly
one deal, the same one it had if it had one; a second deal is reported as
an error, never as success.

The write is never retried (registry: retries=0).
"""
from __future__ import annotations

import re

from transport.mcp_client import INVALID_TRANSITION, NOT_FOUND, MCPClient, MCPToolError

OPEN_STATUSES = ("new", "contacted", "qualified")
# status -> (next status, the transition tool that makes that move)
NEXT_STEP = {
    "new": ("contacted", "Lead.contact"),
    "contacted": ("qualified", "Lead.qualify"),
    "qualified": ("converted", "Lead.convert"),
}
ADVANCE_STATUSES = tuple(status for status, _ in NEXT_STEP.values())
ORDER = ("new", "contacted", "qualified", "converted")
LEAD_FIELDS = ("id", "status", "party_id", "updated_at")
DEAL_FIELDS = ("id", "title", "stage", "lead_id")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in keys}


def lead_deals(client: MCPClient, lead_id: str) -> list[dict]:
    """The lead's deals. The lead_id filter is checked here too: a filter the
    server ignored would return other leads' deals."""
    rows = client.call("Deal.list", {"lead_id": lead_id, "limit": 50}).get("data") or []
    return [_pick(r, DEAL_FIELDS) for r in rows if r.get("lead_id") == lead_id]


def plan_step(status: str | None, target: str | None) -> tuple[tuple[str, str] | None, str | None]:
    """((next status, tool), None) for the one allowed step, or (None, why not)."""
    if status not in OPEN_STATUSES:
        return None, f"it is {status}, so there is nothing to move"
    nxt, tool = NEXT_STEP[status]
    if target in (None, nxt):
        return (nxt, tool), None
    if target == status:
        return None, f"it is already {status}"
    if target in ORDER and ORDER.index(target) < ORDER.index(status):
        return None, f"it is {status}; this agent only moves leads forward, not back to {target}"
    return None, (f"it is {status}, so the next step is {nxt}; going straight to {target} "
                  "would skip a step (a lead is contacted, then qualified, then converted)")


def advance_lead(client: MCPClient, lead_ref: str, target: str | None = None,
                 dry_run: bool = False) -> dict:
    """outcome: advanced (moved and read back), would_advance (dry run),
    refused (see the module doc), error (the move didn't read back)."""
    ref = (lead_ref or "").strip()
    base = {"lead_ref": lead_ref, "target_status": target}
    if not ref:
        return {**base, "outcome": "refused", "reason": "can't move a lead without knowing which one"}
    if not UUID_RE.fullmatch(ref):
        return {**base, "outcome": "refused",
                "reason": f"leads have no title to search by; give the lead id, not {ref!r}"}
    try:
        lead = _pick(client.call("Lead.get", {"id": ref}), LEAD_FIELDS)
    except MCPToolError as e:
        if e.code != NOT_FOUND:
            raise
        return {**base, "outcome": "refused", "reason": f"lead {ref} does not exist"}
    base.update(lead_id=lead["id"], status_before=lead["status"], updated_at_before=lead["updated_at"])
    step, why = plan_step(lead["status"], target)
    if step is None:
        return {**base, "outcome": "refused", "reason": f"lead {ref} was not moved: {why}"}
    nxt, tool = step
    base.update(next_status=nxt, tool=tool)
    existing: list[dict] = []
    if nxt == "converted":
        existing = lead_deals(client, lead["id"])
        base["existing_deals"] = existing
        if len(existing) > 1:
            return {**base, "outcome": "refused",
                    "reason": (f"lead {ref} was not converted: it already has {len(existing)} deals, "
                               "and converting links one of them; say which deal is the lead's")}
    if tool not in {t.get("name") for t in client.list_tools()}:
        return {**base, "outcome": "refused",
                "reason": f"lead {ref} was not moved: no {tool} in this seat's catalogue"}
    if dry_run:
        return {**base, "outcome": "would_advance", "dry_run": True,
                "reason": f"dry run: lead {ref} would move {lead['status']} -> {nxt} with {tool}; "
                          "nothing was written"}
    try:
        client.call(tool, {"id": lead["id"]})
    except MCPToolError as e:
        if e.code != INVALID_TRANSITION:
            raise
        now = _pick(client.call("Lead.get", {"id": lead["id"]}), LEAD_FIELDS)
        return {**base, "outcome": "refused", "status_now": now["status"],
                "reason": f"lead {ref} was {lead['status']} when read but is {now['status']} now: "
                          "it changed underneath this run, so it was not moved"}
    after = _pick(client.call("Lead.get", {"id": lead["id"]}), LEAD_FIELDS)
    if after["status"] != nxt:
        return {**base, "outcome": "error", "status_after": after["status"],
                "reason": f"{tool} returned, but lead {ref} reads back as {after['status']!r}"}
    out = {**base, "outcome": "advanced", "status_after": after["status"],
           "updated_at_after": after["updated_at"],
           "reason": f"moved lead {ref} {lead['status']} -> {nxt} with {tool}; read back as {nxt}"}
    if nxt == "converted":
        deals = lead_deals(client, lead["id"])
        out["deals"] = deals
        if len(deals) != 1:
            return {**out, "outcome": "error",
                    "reason": f"lead {ref} reads back as converted, but has {len(deals)} deal(s), not 1"}
        if existing and deals[0]["id"] != existing[0]["id"]:
            return {**out, "outcome": "error",
                    "reason": (f"lead {ref} reads back as converted, but its deal is now "
                               f"{deals[0]['title']!r}, not the deal it had ({existing[0]['title']!r})")}
        out.update(deal_id=deals[0]["id"], deal_title=deals[0]["title"], deal_linked=bool(existing),
                   reason=out["reason"] + (f"; it linked the deal it already had, {deals[0]['title']!r}"
                                           if existing else f"; it created deal {deals[0]['title']!r}"))
    return out
