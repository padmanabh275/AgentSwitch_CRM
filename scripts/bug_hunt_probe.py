"""Live bug hunt against AgentSwitch (Team 6 seat). Evidence-first, write-guarded.

Run from the repo root with the agent package on the path:
    $env:PYTHONPATH = "agent"
    python scripts/bug_hunt_probe.py read      # Phase A: read-only sweeps
    python scripts/bug_hunt_probe.py write     # Phase B: T6-hunt- write probes
    python scripts/bug_hunt_probe.py confirm   # Phase C: reproduce every candidate

Every call and response is appended to docs/bug-hunt-2026-09-28.jsonl.
Candidates from each phase go to runs/bug-hunt/<phase>.json; records the
write phase created go to runs/bug-hunt/state.json.

Write guard (GuardedClient): a create must carry a T6-hunt- title/name/
subject/content; an update may only target a record this hunt created or
the known E2 probe deal; anything else that isn't .list/.get is refused
before it leaves the process. At most WRITE_BUDGET records are created.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from config import REPO_DIR, load_env
from harness.run_record import read_json, write_json
from harness.verifiers import _page_all
from transport.mcp_client import MCPToolError, client_from_env

PREFIX = "T6-hunt-"
LABEL_FIELDS = ("title", "name", "subject", "content", "notes")
WRITE_BUDGET = 15
E2_PROBE_DEAL = "bf83f75b-74b8-424a-8eba-acabc58130bd"
COMPANY_ID = "5cbe5a55-af74-4363-a436-f5350593114c"
EVIDENCE = REPO_DIR / "docs" / "bug-hunt-2026-09-28.jsonl"
STATE_DIR = REPO_DIR / "runs" / "bug-hunt"
STATE = STATE_DIR / "state.json"
NONSENSE = "zzqx-t6hunt-no-such-thing"
MISSING_ID = "7e57c0de-0000-4000-8000-00000000beef"
PAGING_KEYS = {"limit", "offset", "sort_by", "sort_order", "search"}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


class WriteRefused(Exception):
    pass


def _brief(result):
    """Keep the evidence log readable: first rows of a page, full single records."""
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        return {**{k: v for k, v in result.items() if k != "data"},
                "data_len": len(result["data"]), "data_head": result["data"][:2]}
    return result


class GuardedClient:
    def __init__(self, client, phase: str, state: dict):
        self._client = client
        self.phase = phase
        self.state = state
        self.calls = 0

    def list_tools(self):
        return self._client.list_tools()

    def _check_write(self, name: str, args: dict) -> None:
        if name.startswith("BugReport."):
            raise WriteRefused(f"{name}: filing is out of scope for the hunt")
        if name.endswith(".create"):
            labels = [args.get(k) for k in LABEL_FIELDS]
            if not any(isinstance(v, str) and v.startswith(PREFIX) for v in labels):
                raise WriteRefused(f"{name}: create without a {PREFIX} label")
            if len(self.state["created"]) >= WRITE_BUDGET:
                raise WriteRefused(f"{name}: write budget of {WRITE_BUDGET} spent")
            return
        if name.endswith(".update"):
            ours = {r["id"] for r in self.state["created"]} | {E2_PROBE_DEAL}
            if args.get("id") not in ours:
                raise WriteRefused(f"{name}: {args.get('id')} was not created by this hunt")
            return
        raise WriteRefused(f"{name}: only .list/.get/.create/.update are allowed")

    def call(self, name: str, args: dict | None = None, probe: str = "") -> dict:
        args = args or {}
        if not (name.endswith(".list") or name.endswith(".get")):
            self._check_write(name, args)
        self.calls += 1
        entry = {"ts": dt.datetime.now(IST).isoformat(), "phase": self.phase, "probe": probe,
                 "tool": name, "args": args}
        try:
            result = self._client.call(name, args)
        except MCPToolError as e:
            data = ((e.raw or {}).get("error") or {}).get("data") or {}
            _log({**entry, "ok": False, "error_code": e.code, "error": str(e)[:600],
                  "error_data": data})
            raise
        _log({**entry, "ok": True, "result": _brief(result)})
        if name.endswith(".create") and isinstance(result, dict) and result.get("id"):
            self.state["created"].append({"tool": name, "id": result["id"], "probe": probe,
                                          "label": next((args[k] for k in LABEL_FIELDS
                                                         if isinstance(args.get(k), str)
                                                         and args[k].startswith(PREFIX)), None)})
            write_json(STATE, self.state)
        return result

    def try_call(self, name: str, args: dict | None = None, probe: str = "") -> dict:
        """{"ok": True, "result": ...} or {"ok": False, "code": ..., "message": ...}."""
        try:
            return {"ok": True, "result": self.call(name, args, probe)}
        except MCPToolError as e:
            data = ((e.raw or {}).get("error") or {}).get("data") or {}
            return {"ok": False, "code": e.code, "message": str(e)[:400], "data": data}


def _log(entry: dict) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _schema(tools: dict, name: str) -> dict:
    return ((tools.get(name) or {}).get("inputSchema") or {}).get("properties") or {}


def _entities(tools: dict, suffix: str) -> list[str]:
    return sorted(n[: -len(suffix)] for n in tools if n.endswith(suffix) and n.count(".") == 1)


# ---------------------------------------------------------------- Phase A

def sweep_update_vs_readonly(c: GuardedClient, tools: dict) -> list[dict]:
    """Update schemas that offer fields the records themselves mark read-only,
    and state fields (stage/status) offered with no enum."""
    found = []
    for ent in _entities(tools, ".update"):
        props = _schema(tools, f"{ent}.update")
        if f"{ent}.list" not in tools:
            continue
        r = c.try_call(f"{ent}.list", {"limit": 1}, "A1 readonly")
        rows = (r["result"].get("data") or []) if r["ok"] else []
        readonly = set((rows[0].get("_readonly_fields") or []) if rows else [])
        overlap = sorted((set(props) & readonly) - {"id"})
        transitions = sorted({n for n in tools if n.startswith(f"{ent}.") and n.count(".") >= 1
                              and n.split(".")[1] not in ("list", "get", "create", "update", "make")
                              and not n.split(".")[1].startswith(("export", "import"))})
        state_no_enum = sorted(k for k in ("stage", "status") if k in props and "enum" not in props[k])
        if overlap or (state_no_enum and transitions):
            found.append({"entity": ent, "update_offers_readonly": overlap,
                          "state_fields_without_enum": state_no_enum if transitions else [],
                          "transition_tools": transitions[:12]})
    return found


def sweep_schema_consistency(c: GuardedClient, tools: dict) -> dict:
    sort_types = defaultdict(list)
    for ent in _entities(tools, ".list"):
        sort_types[json.dumps(_schema(tools, f"{ent}.list").get("sort_order"), sort_keys=True)].append(ent)
    minority = min(sort_types.values(), key=len) if len(sort_types) > 1 else []
    sort_probe = {}
    for ent in minority[:3]:
        sort_probe[ent] = {v: _outcome(c.try_call(f"{ent}.list", {"limit": 3, "sort_order": v}, "A2 sort_order"))
                           for v in ("desc", "asc", 1)}
    for ent in ("Deal", "Lead"):
        sort_probe[ent] = {v: _outcome(c.try_call(f"{ent}.list", {"limit": 3, "sort_order": v}, "A2 sort_order"))
                           for v in ("desc", 1)}
    caps = {}
    for ent in ("Deal", "Lead", "SalesOrder", "Party", "Item", "Quotation"):
        declared = _schema(tools, f"{ent}.list").get("limit", {}).get("maximum")
        r = c.try_call(f"{ent}.list", {"limit": 1000}, "A2 limit cap")
        if r["ok"]:
            caps[ent] = {"declared_max": declared, "returned": len(r["result"].get("data") or []),
                         "total": r["result"].get("total")}
    return {"sort_order_shapes": {k: v for k, v in sort_types.items()}, "sort_order_probe": sort_probe,
            "limit_caps": caps}


def _outcome(r: dict) -> str:
    if r["ok"]:
        res = r["result"]
        return f"ok total={res.get('total')} n={len(res.get('data') or [])}"
    return f"{r['code']}: {r['message'][:160]}"


def sweep_filter_defaults(c: GuardedClient, tools: dict) -> list[dict]:
    """A list schema that advertises defaults on its filters: does sending
    those defaults (as a schema-driven client would) change the result?"""
    found = []
    for ent in _entities(tools, ".list"):
        props = _schema(tools, f"{ent}.list")
        defaults = {k: v["default"] for k, v in props.items()
                    if "default" in v and k not in PAGING_KEYS}
        if not defaults:
            continue
        base = c.try_call(f"{ent}.list", {"limit": 1}, "A3 defaults baseline")
        with_defaults = c.try_call(f"{ent}.list", {"limit": 1, **defaults}, "A3 defaults sent")
        if not base["ok"]:
            continue
        found.append({"entity": ent, "defaults": defaults,
                      "total_without": base["result"].get("total"),
                      "with_defaults": _outcome(with_defaults)})
    return found


def sweep_filters_honoured(c: GuardedClient, tools: dict) -> list[dict]:
    """Nonsense search should match nothing; a filter set to a value taken
    from a real row should return only rows carrying that value."""
    found = []
    for ent in _entities(tools, ".list"):
        props = _schema(tools, f"{ent}.list")
        base = c.try_call(f"{ent}.list", {"limit": 50}, "A4 filter baseline")
        if not base["ok"]:
            continue
        rows = base["result"].get("data") or []
        total = base["result"].get("total")
        rec = {"entity": ent, "total": total, "ignored": [], "bogus_enum_accepted": []}
        if "search" in props:
            s = c.try_call(f"{ent}.list", {"limit": 5, "search": NONSENSE}, "A4 nonsense search")
            if s["ok"] and total and s["result"].get("total") == total:
                rec["ignored"].append({"filter": "search", "sent": NONSENSE,
                                       "total": s["result"].get("total")})
        candidates = [k for k, v in props.items() if k not in PAGING_KEYS
                      and v.get("type") in ("string", "boolean") and k != "company_id"]
        tested = 0
        for key in candidates:
            values = Counter(r.get(key) for r in rows if isinstance(r.get(key), (str, bool)))
            if len(values) < 2:
                continue
            value, _ = values.most_common()[-1]
            r = c.try_call(f"{ent}.list", {"limit": 50, key: value}, f"A4 filter {key}")
            tested += 1
            if r["ok"]:
                got = r["result"].get("data") or []
                wrong = [x.get(key) for x in got if x.get(key) != value]
                if wrong:
                    rec["ignored"].append({"filter": key, "sent": value, "total": r["result"].get("total"),
                                           "rows_not_matching": len(wrong), "of": len(got),
                                           "sample_values": sorted({str(w) for w in wrong})[:5]})
            if tested >= 4:
                break
        enum_key = next((k for k, v in props.items() if "enum" in v and k not in PAGING_KEYS), None)
        if enum_key:
            r = c.try_call(f"{ent}.list", {"limit": 1, enum_key: "zz_bogus"}, f"A4 bogus enum {enum_key}")
            if r["ok"]:
                rec["bogus_enum_accepted"].append({"filter": enum_key, "total": r["result"].get("total")})
        if rec["ignored"] or rec["bogus_enum_accepted"]:
            found.append(rec)
    return found


def sweep_pagination(c: GuardedClient, tools: dict) -> dict:
    out = {}
    for ent in ("Deal", "Lead", "Party", "Item"):
        first = c.try_call(f"{ent}.list", {"limit": 50, "offset": 0}, "A5 paging")
        if not first["ok"]:
            continue
        total = first["result"].get("total") or 0
        out[ent] = {
            "total": total,
            "offset_past_end": _outcome(c.try_call(f"{ent}.list", {"limit": 5, "offset": total + 10}, "A5 past end")),
            "limit_0": _outcome(c.try_call(f"{ent}.list", {"limit": 0}, "A5 limit 0")),
            "offset_negative": _outcome(c.try_call(f"{ent}.list", {"limit": 5, "offset": -1}, "A5 negative")),
            "unknown_sort_by": _outcome(c.try_call(f"{ent}.list", {"limit": 5, "sort_by": "zz_no_such_field"}, "A5 sort_by")),
        }
    return out


def sweep_data_vs_enum(c: GuardedClient, tools: dict) -> list[dict]:
    found = []
    for ent in _entities(tools, ".list"):
        enums = {}
        for tool in (f"{ent}.create", f"{ent}.update", f"{ent}.list"):
            for k, v in _schema(tools, tool).items():
                if "enum" in v:
                    enums.setdefault(k, set()).update(v["enum"])
        if not enums:
            continue
        r = c.try_call(f"{ent}.list", {"limit": 50}, "A6 data vs enum")
        if not r["ok"]:
            continue
        for k, allowed in enums.items():
            bad = Counter(str(row.get(k)) for row in r["result"].get("data") or []
                          if row.get(k) not in (None, "") and row.get(k) not in allowed)
            if bad:
                found.append({"entity": ent, "field": k, "allowed": sorted(allowed),
                              "out_of_enum": dict(bad)})
    return found


def sweep_error_envelopes(c: GuardedClient, tools: dict) -> dict:
    shapes = defaultdict(list)
    for ent in _entities(tools, ".get"):
        for label, value in (("missing_uuid", MISSING_ID), ("malformed", "not-a-uuid")):
            r = c.try_call(f"{ent}.get", {"id": value}, f"A7 get {label}")
            key = "ok(!)" if r["ok"] else r["code"]
            shapes[f"{label}:{key}"].append(ent)
    return {k: v for k, v in shapes.items()}


def sweep_escalations(c: GuardedClient, tools: dict) -> dict:
    rows, _ = _page_all(c, "AgentEscalation.list", {})
    by_num = {r.get("number"): r for r in rows}
    ours, theirs = by_num.get("ESC-2026-00026"), by_num.get("ESC-2026-00025")
    if not (ours and theirs):
        return {"error": "one of ESC-2026-00025/00026 not visible", "visible": sorted(by_num)[:40]}
    skip = {"id", "number", "created_at", "updated_at", "subject", "reason", "session_id"}
    diff = {k: {"ours_00026": ours.get(k), "team04_00025": theirs.get(k)}
            for k in sorted(set(ours) | set(theirs)) if k not in skip and ours.get(k) != theirs.get(k)}
    assigned = Counter(bool(r.get("assignee_user_id")) for r in rows)
    return {"diff": diff, "visible": len(rows), "assigned_count": assigned.get(True, 0),
            "unassigned_count": assigned.get(False, 0)}


def phase_read(c: GuardedClient) -> dict:
    tools = {t["name"]: t for t in c.list_tools()}
    return {
        "tool_count": len(tools),
        "update_vs_readonly": sweep_update_vs_readonly(c, tools),
        "schema_consistency": sweep_schema_consistency(c, tools),
        "filter_defaults": sweep_filter_defaults(c, tools),
        "filters_honoured": sweep_filters_honoured(c, tools),
        "pagination": sweep_pagination(c, tools),
        "data_vs_enum": sweep_data_vs_enum(c, tools),
        "error_envelopes": sweep_error_envelopes(c, tools),
        "escalations": sweep_escalations(c, tools),
    }


# ---------------------------------------------------------------- Phase B

def _create_and_read(c: GuardedClient, entity: str, args: dict, probe: str, fields: list[str]) -> dict:
    earlier = next((r for r in c.state["created"] if r["probe"] == probe), None)
    if earlier:
        rid = earlier["id"]
    else:
        r = c.try_call(f"{entity}.create", args, probe)
        if not r["ok"]:
            return {"probe": probe, "accepted": False, "code": r["code"], "message": r["message"]}
        rid = r["result"].get("id")
    back = c.try_call(f"{entity}.get", {"id": rid}, probe + " readback")
    rec = back["result"] if back["ok"] else {}
    return {"probe": probe, "accepted": True, "id": rid,
            "sent": {f: args.get(f) for f in fields},
            "stored": {f: rec.get(f) for f in fields}}


def phase_write(c: GuardedClient) -> dict:
    out: dict = {"probes": []}
    party = next((r for r in c.state["created"] if r["tool"] == "Party.create"), None)
    if party:
        party_id = party["id"]
    else:
        pr = c.call("Party.create", {"name": f"{PREFIX}party", "type": "organization"}, "B0 party")
        party_id = pr["id"]
    out["party_id"] = party_id
    stamp = dt.datetime.now(IST).strftime("%H%M")

    deal_probes = [
        ("B1 probability 150", {"probability": 150}, ["probability"]),
        ("B2 probability -5", {"probability": -5}, ["probability"]),
        ("B3 close date 2026-02-30", {"expected_close_date": "2026-02-30"}, ["expected_close_date"]),
        ("B4 negative value", {"value": -1000}, ["value"]),
        ("B5 unknown currency", {"currency": "ZZZ"}, ["currency"]),
        ("B6 create straight into closed_won", {"stage": "closed_won", "value": 1}, ["stage", "value"]),
    ]
    for probe, extra, fields in deal_probes:
        args = {"title": f"{PREFIX}{probe.split()[0]}-{stamp}", "party_id": party_id, **extra}
        out["probes"].append(_create_and_read(c, "Deal", args, probe, fields))

    out["probes"].append(_create_and_read(
        c, "Note", {"content": f"{PREFIX}B7 note on a party that does not exist", "party_id": MISSING_ID},
        "B7 note with nonexistent party_id", ["party_id"]))
    out["probes"].append(_create_and_read(
        c, "Activity", {"subject": f"{PREFIX}B8 activity", "type": "task", "due_date": "2026-10-01",
                        "party_id": MISSING_ID, "deal_id": MISSING_ID},
        "B8 activity with nonexistent party_id/deal_id", ["party_id", "deal_id"]))
    out["probes"].append(_create_and_read(
        c, "Quotation", {"party_id": party_id, "company_id": COMPANY_ID, "date": "2026-10-10",
                         "valid_till": "2026-09-01", "notes": f"{PREFIX}B9"},
        "B9 quotation valid_till before date", ["date", "valid_till"]))
    out["probes"].append(_create_and_read(
        c, "Quotation", {"party_id": party_id, "company_id": COMPANY_ID, "approval_status": "approved",
                         "status": "accepted", "notes": f"{PREFIX}B10"},
        "B10 quotation created already approved and accepted", ["approval_status", "status"]))

    quote_b9 = next((p for p in out["probes"] if p["probe"].startswith("B9") and p.get("accepted")), None)
    if quote_b9:
        upd = c.try_call("Quotation.update", {"id": quote_b9["id"], "status": "accepted"},
                         "B12 quotation status via update")
        back = c.try_call("Quotation.get", {"id": quote_b9["id"]}, "B12 readback")
        out["probes"].append({"probe": "B12 Quotation.update status=accepted on a draft", "id": quote_b9["id"],
                              "accepted": upd["ok"], "code": None if upd["ok"] else upd["code"],
                              "message": None if upd["ok"] else upd["message"],
                              "stored": {"status": (back.get("result") or {}).get("status")}})

    earlier = next((r for r in c.state["created"] if r["probe"] == "B11 session"), None)
    sess = ({"ok": True, "result": {"id": earlier["id"]}} if earlier else
            c.try_call("AgentSession.create", {"title": f"{PREFIX}session", "channel": "api"}, "B11 session"))
    if sess["ok"]:
        sid = sess["result"]["id"]
        upd = c.try_call("AgentSession.update", {"id": sid, "total_tool_calls": -7, "estimated_cost": -3.5,
                                                 "total_input_tokens": -100}, "B11 negative counters")
        back = c.try_call("AgentSession.get", {"id": sid}, "B11 readback")
        out["probes"].append({"probe": "B11 session counters written negative", "id": sid,
                              "accepted": upd["ok"], "code": None if upd["ok"] else upd["code"],
                              "stored": {k: (back.get("result") or {}).get(k) for k in
                                         ("total_tool_calls", "estimated_cost", "total_input_tokens")}})
        upd = c.try_call("AgentSession.update", {"id": sid, "status": "closed"}, "B13 session status via update")
        back = c.try_call("AgentSession.get", {"id": sid}, "B13 readback")
        out["probes"].append({"probe": "B13 AgentSession.update status=closed", "id": sid,
                              "accepted": upd["ok"], "code": None if upd["ok"] else upd["code"],
                              "message": None if upd["ok"] else upd["message"],
                              "stored": {"status": (back.get("result") or {}).get("status")}})
    return out


# ---------------------------------------------------------------- Phase C

def targeted_checks(c: GuardedClient, tools: dict) -> dict:
    """One narrow reproduction per Phase A/B candidate."""
    out: dict = {}

    per_default = {}
    for ent in ("Deal", "Party", "Quotation", "SalesOrder", "AgentSession"):
        props = _schema(tools, f"{ent}.list")
        per_default[ent] = {k: {"default": v["default"],
                                "outcome": _outcome(c.try_call(f"{ent}.list", {"limit": 1, k: v["default"]},
                                                               f"C default {k}"))}
                            for k, v in props.items() if "default" in v and k not in PAGING_KEYS}
        per_default[ent]["_baseline"] = _outcome(c.try_call(f"{ent}.list", {"limit": 1}, "C default baseline"))
    out["filter_default_per_field"] = per_default

    out["sort_order_minority"] = {
        ent: {"schema": _schema(tools, f"{ent}.list").get("sort_order"),
              **{repr(v): _outcome(c.try_call(f"{ent}.list", {"limit": 3, "sort_order": v}, "C sort_order"))
                 for v in (0, 1, -1, "desc")}}
        for ent in ("Item", "Pipeline", "AddressBook") if f"{ent}.list" in tools}

    prov_schema = {t: _schema(tools, f"AgentProvider.{t}").get("provider") for t in ("list", "create", "update")}
    out["agent_provider"] = {
        "provider_schema": prov_schema,
        "list_gemini": _outcome(c.try_call("AgentProvider.list", {"limit": 5, "provider": "gemini"}, "C provider")),
        "list_fireworks": _outcome(c.try_call("AgentProvider.list", {"limit": 5, "provider": "fireworks"},
                                              "C provider")),
        "list_all": _outcome(c.try_call("AgentProvider.list", {"limit": 50}, "C provider")),
    }

    out["search"] = {ent: {"baseline": _outcome(c.try_call(f"{ent}.list", {"limit": 5}, "C search")),
                           "nonsense": _outcome(c.try_call(f"{ent}.list", {"limit": 5, "search": NONSENSE},
                                                           "C search")),
                           "schema": _schema(tools, f"{ent}.list").get("search")}
                     for ent in ("ContactGroupMember", "Lead", "Deal", "Party")}

    rows, _ = _page_all(c, "BugReport.list", {})
    seats = Counter(r.get("agent_seat") for r in rows)
    seat = next((s for s, _ in seats.most_common()[::-1] if s), None)
    if seat:
        r = c.try_call("BugReport.list", {"limit": 50, "agent_seat": seat}, "C agent_seat")
        got = (r["result"].get("data") or []) if r["ok"] else []
        out["bugreport_agent_seat"] = {"sent": seat, "seat_counts": {str(k): v for k, v in seats.items()},
                                       "returned": len(got),
                                       "not_matching": sum(1 for x in got if x.get("agent_seat") != seat)}

    esc, _ = _page_all(c, "AgentEscalation.list", {})
    out["escalation_tally"] = dict(Counter(
        f"channel={'set' if e.get('channel') else 'null'} raised_at={'set' if e.get('raised_at') else 'null'} "
        f"assignee={'set' if e.get('assignee_user_id') else 'null'} sla={'set' if e.get('sla_minutes') else 'null'}"
        for e in esc))
    out["escalation_create_schema"] = {k: v for k, v in _schema(tools, "AgentEscalation.create").items()
                                       if k in ("channel", "raised_by", "raised_at", "sla_minutes", "priority")}
    out["escalation_create_required"] = (tools.get("AgentEscalation.create") or {}).get(
        "inputSchema", {}).get("required")

    out["unknown_sort_by"] = {ent: c.try_call(f"{ent}.list", {"limit": 1, "sort_by": "zz_no_such_field"},
                                              "C sort_by") for ent in ("Deal", "Party")}

    out["limits"] = {ent: {"no_limit": _outcome(c.try_call(f"{ent}.list", {}, "C limit")),
                           "limit_1000": _outcome(c.try_call(f"{ent}.list", {"limit": 1000}, "C limit"))}
                     for ent in ("Deal", "SalesOrder")}

    out["write_schemas"] = {
        "Deal.create.currency": _schema(tools, "Deal.create").get("currency"),
        "Deal.create.stage": _schema(tools, "Deal.create").get("stage"),
        "Quotation.create.status": _schema(tools, "Quotation.create").get("status"),
        "AgentSession.update.counters": {k: _schema(tools, "AgentSession.update").get(k)
                                         for k in ("total_tool_calls", "estimated_cost", "total_input_tokens")},
        "AgentSession.update.status": _schema(tools, "AgentSession.update").get("status"),
    }
    out["transition_tools"] = sorted(n for n in tools if n.split(".")[0] in ("Deal", "Quotation", "AgentSession")
                                     and n.split(".")[-1] not in ("list", "get", "create", "update"))
    out["currencies_in_deals"] = dict(Counter(str(d.get("currency")) for d in _page_all(c, "Deal.list", {})[0]))
    return out


def phase_confirm(c: GuardedClient) -> dict:
    """Re-run the reads behind each candidate; re-read every created record so
    the persisted value is shown twice, on two separate runs."""
    tools = {t["name"]: t for t in c.list_tools()}
    out = {"targeted": targeted_checks(c, tools),
           "filter_defaults": sweep_filter_defaults(c, tools),
           "filters_honoured": sweep_filters_honoured(c, tools),
           "update_vs_readonly": sweep_update_vs_readonly(c, tools),
           "schema_consistency": sweep_schema_consistency(c, tools),
           "records": []}
    for rec in c.state["created"]:
        ent = rec["tool"].split(".")[0]
        r = c.try_call(f"{ent}.get", {"id": rec["id"]}, "C reread")
        out["records"].append({**rec, "reread": r["result"] if r["ok"] else {"error": r["code"]}})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["read", "write", "confirm"])
    args = ap.parse_args()
    state = read_json(STATE) if STATE.exists() else {"created": []}
    c = GuardedClient(client_from_env(load_env()), args.phase, state)
    runner = {"read": phase_read, "write": phase_write, "confirm": phase_confirm}[args.phase]
    result = runner(c)
    path = write_json(STATE_DIR / f"{args.phase}.json", result)
    print(json.dumps(result, indent=1, default=str)[:20000])
    print(f"\n[{c.calls} calls; {len(state['created'])} records created so far; saved {path}]",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
