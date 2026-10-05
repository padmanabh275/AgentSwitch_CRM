"""Verifiers: ground truth from AgentSwitch itself, never from the agent.

Each verifier has two halves:

- observation keys it needs. The runner reads those from the platform with
  its *own* login, before and after the agent runs, and writes them to
  runs/<run_id>/ground_truth_{before,after}.json. Nothing is scored then.
- `check(files, params)`: a pure function over the saved run directory
  (task.json, taskrun.json, graph.json / chat_trace.json, calls.jsonl and
  the two ground-truth files). score.py calls it; it never touches the
  network, so a scorer bug is fixed by re-scoring saved runs.

The rules ("closing this month", "at risk", how an item reference resolves,
what a quote should come to) are re-derived here from raw rows on purpose,
not imported from domain/: a bug in the agent's filter must not be able to
pass its own check. The agent's snapshot in graph.json is never used as
ground truth for the same reason.

Check statuses: pass | fail | drift (the book moved under the run; see
deal_set_matches_db) | skip (not applicable) | error (ground truth could
not be read, or the task's premise doesn't hold on live data).
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from harness.call_log import read_calls
from harness.run_record import read_json
from transport.mcp_client import TRANSIENT, MCPToolError

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
OPEN_STAGES = ("new", "qualification", "proposal", "negotiation")
T6_PREFIX = "T6-"
T6_SESSION_TITLE = "T6-Sales pipeline agent run"
TERMINAL_ESCALATION = ("withdrawn", "resolved", "closed", "cancelled", "rejected")
WRITE_ALLOWED = ("AgentSession.create", "AgentSession.update", "AgentMemory.create",
                 "AgentEscalation.create")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MONEY_TOLERANCE = 1.0
PAGE = 50
MAX_PAGES = 40
LIST_LIMIT = 10   # ids shown per mismatch list in a check's detail
OBSERVE_ATTEMPTS = 3
OBSERVE_BACKOFF_S = 1.0


# ---------------------------------------------------------------- observe

def _page_all(client, tool: str, args: dict) -> tuple[list[dict], int | None]:
    by_id: dict = {}
    offset, total = 0, None
    for _ in range(MAX_PAGES):
        page = client.call(tool, {**args, "limit": PAGE, "offset": offset})
        rows = page.get("data") or []
        total = page.get("total")
        for r in rows:
            by_id[r.get("id")] = r
        offset += len(rows)
        if not rows or (total is not None and offset >= total) or (total is None and len(rows) < PAGE):
            break
    return list(by_id.values()), total


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in keys}


DEAL_KEYS = ("id", "title", "stage", "expected_close_date", "value", "currency", "updated_at")
ITEM_KEYS = ("id", "name", "code", "standard_rate", "default_bom_id", "is_sellable")
ESC_KEYS = ("id", "number", "subject", "status", "session_id", "created_at")


def _obs_deals(client, _arg: str) -> dict:
    rows, total = _page_all(client, "Deal.list", {})
    return {"total": total, "rows": [_pick(r, DEAL_KEYS) for r in rows]}


def _obs_item_ref(client, ref: str) -> dict:
    if UUID_RE.fullmatch(ref.strip()):
        return {"kind": "get", "row": _pick(client.call("Item.get", {"id": ref.strip()}), ITEM_KEYS)}
    page = client.call("Item.list", {"search": ref.strip(), "limit": 20})
    return {"kind": "search", "total": page.get("total"),
            "rows": [_pick(r, ITEM_KEYS) for r in page.get("data") or []]}


def _obs_escalations(client, _arg: str) -> dict:
    rows, total = _page_all(client, "AgentEscalation.list", {})
    return {"total": total,
            "rows": [_pick(r, ESC_KEYS) for r in rows
                     if str(r.get("subject") or "").startswith(T6_PREFIX)]}


def _obs_sessions(client, _arg: str) -> dict:
    page = client.call("AgentSession.list", {"title": T6_SESSION_TITLE, "limit": 1})
    return {"total": page.get("total")}


def _obs_tools(client, _arg: str) -> dict:
    return {"names": sorted(t.get("name") for t in client.list_tools())}


def _obs_record(client, arg: str) -> dict:
    entity, record_id = arg.split(":", 1)
    return {"row": {"id": client.call(f"{entity}.get", {"id": record_id}).get("id")}}


OBSERVERS: dict[str, Callable] = {
    "deals": _obs_deals,
    "item_ref": _obs_item_ref,
    "escalations_t6": _obs_escalations,
    "sessions_t6": _obs_sessions,
    "tools_list": _obs_tools,
    "record": _obs_record,
}


def observe(client, key: str) -> dict:
    """One observation. A platform error is recorded (with its code), not
    raised: `not_found` is itself ground truth for the refusal tasks.
    Transient failures are retried — every observer is a read."""
    kind, _, arg = key.partition(":")
    for attempt in range(OBSERVE_ATTEMPTS):
        try:
            return OBSERVERS[kind](client, arg)
        except MCPToolError as e:
            if e.code == TRANSIENT and attempt < OBSERVE_ATTEMPTS - 1:
                time.sleep(OBSERVE_BACKOFF_S * 2 ** attempt)
                continue
            return {"error_code": e.code, "error": str(e)[:300], "attempts": attempt + 1}
        except Exception as e:
            return {"error_code": "internal", "error": f"{type(e).__name__}: {e}"[:300]}


def observe_all(client, keys: list[str]) -> dict:
    return {"observed_at": dt.datetime.now(IST).isoformat(),
            "observations": {k: observe(client, k) for k in keys}}


# ---------------------------------------------------------------- run files

@dataclass
class Check:
    verifier: str
    name: str
    status: str          # pass | fail | drift | skip | error
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunFiles:
    run_dir: Path
    task: dict
    taskrun: dict
    graph: dict | None
    chat_trace: list | None
    calls: list[dict]
    before: dict
    after: dict

    @classmethod
    def load(cls, run_dir: Path) -> "RunFiles":
        def opt(name):
            p = run_dir / name
            return read_json(p) if p.exists() else None
        taskrun = read_json(run_dir / "taskrun.json")
        task = opt("task.json") or {"id": taskrun.get("harness_task") or "adhoc", "verifiers": []}
        return cls(run_dir=run_dir, task=task, taskrun=taskrun,
                   graph=opt("graph.json"), chat_trace=opt("chat_trace.json"),
                   calls=read_calls(run_dir / "calls.jsonl"),
                   before=opt("ground_truth_before.json") or {},
                   after=opt("ground_truth_after.json") or {})

    @property
    def finding(self) -> dict:
        return self.taskrun.get("finding") or {}

    @property
    def dry_run(self) -> bool:
        return bool(self.taskrun.get("dry_run"))

    def obs(self, phase: str, key: str) -> dict | None:
        src = self.before if phase == "before" else self.after
        return (src.get("observations") or {}).get(key)

    def today(self) -> dt.date:
        if self.taskrun.get("today"):
            return dt.date.fromisoformat(self.taskrun["today"])
        return dt.datetime.fromtimestamp(self.taskrun.get("started_at") or 0, IST).date()


def _ids(xs) -> str:
    xs = sorted(xs)
    return ", ".join(xs[:LIST_LIMIT]) + (f" (+{len(xs) - LIST_LIMIT} more)" if len(xs) > LIST_LIMIT else "")


def _date(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _ts(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _obs_error(o: dict | None) -> str | None:
    if o is None:
        return "not observed"
    return o.get("error_code")


# ---------------------------------------------------------------- universal checks

def check_run_completed(f: RunFiles, _p: dict) -> list[Check]:
    ended = f.taskrun.get("ended")
    if ended == "done":
        return [Check("run_completed", "ended", "pass", "run ended done")]
    if ended == "running":
        return [Check("run_completed", "ended", "fail", "record still says running: the run crashed")]
    return [Check("run_completed", "ended", "fail", f"ended={ended}: {f.taskrun.get('error')}")]


def check_expectations(f: RunFiles, _p: dict) -> list[Check]:
    expect = f.task.get("expect") or {}
    out: list[Check] = []
    for name, want in (expect.get("sections") or {}).items():
        got = (f.finding.get(name) or {}).get("outcome")
        out.append(Check("expectations", f"section:{name}", "pass" if got == want else "fail",
                         f"expected {want}, got {got}"))
    if "refusals_min" in expect:
        n = len(f.finding.get("refusals") or [])
        route = (f.taskrun.get("intent") or {}).get("route")
        out.append(Check("expectations", "refusals", "pass" if n >= expect["refusals_min"] else "fail",
                         f"{n} refusal(s) recorded in the finding (route={route})"))
    for tool, want in (expect.get("chat_tools") or {}).items():
        calls = [t for t in (f.chat_trace or []) if t.get("tool") == tool]
        got = calls[-1].get("outcome") if calls else None
        out.append(Check("expectations", f"chat:{tool}", "pass" if got == want else "fail",
                         f"expected {want}, got {got}" + ("" if calls else " (tool never called)")))
    return out


def _is_read(tool: str) -> bool:
    return tool.endswith(".list") or tool.endswith(".get")


def check_calls_policy(f: RunFiles, _p: dict) -> list[Check]:
    """From calls.jsonl — what the transport saw, not what the agent says."""
    tools = [c.get("tool") for c in f.calls]
    allowed = (lambda t: _is_read(t)) if f.dry_run else (lambda t: _is_read(t) or t in WRITE_ALLOWED)
    bad = sorted({t for t in tools if not allowed(t)})
    mode = "dry run" if f.dry_run else "write mode"
    if bad:
        return [Check("calls_policy", "tools", "fail", f"{mode}: called {', '.join(bad)}")]
    writes = [t for t in tools if not _is_read(t)]
    return [Check("calls_policy", "tools", "pass",
                  f"{mode}: {len(tools)} call(s), writes: {', '.join(writes) or 'none'}")]


def check_no_side_effects(f: RunFiles, _p: dict) -> list[Check]:
    """T6 sessions and escalations, counted on the platform before and after."""
    out = []
    sb, sa = f.obs("before", "sessions_t6"), f.obs("after", "sessions_t6")
    if _obs_error(sb) or _obs_error(sa):
        out.append(Check("no_side_effects", "sessions", "error",
                         f"session count unavailable: {_obs_error(sb) or _obs_error(sa)}"))
    else:
        grew = (sa.get("total") or 0) - (sb.get("total") or 0)
        limit = 0 if f.dry_run else 1
        out.append(Check("no_side_effects", "sessions", "pass" if 0 <= grew <= limit else "fail",
                         f"T6 sessions {sb.get('total')} -> {sa.get('total')} (allowed growth {limit})"))
    eb, ea = f.obs("before", "escalations_t6"), f.obs("after", "escalations_t6")
    if _obs_error(eb) or _obs_error(ea):
        out.append(Check("no_side_effects", "escalations", "error",
                         f"escalation list unavailable: {_obs_error(eb) or _obs_error(ea)}"))
    else:
        new = {r["id"] for r in ea["rows"]} - {r["id"] for r in eb["rows"]}
        limit = 0 if f.dry_run else 1
        out.append(Check("no_side_effects", "escalations", "pass" if len(new) <= limit else "fail",
                         f"{len(new)} new T6 escalation(s) (allowed {limit})"
                         + (f": {_ids(new)}" if new else "")))
    return out


# ---------------------------------------------------------------- deal lists

def _rule_ids(section: str, rows: list[dict], today: dt.date) -> set[str]:
    ids = set()
    for r in rows:
        if r.get("stage") not in OPEN_STAGES:
            continue
        close = _date(r.get("expected_close_date"))
        if close is None:
            continue
        if section == "closing_this_month" and (close.year, close.month) == (today.year, today.month):
            ids.add(r["id"])
        elif section == "at_risk" and close < today:
            ids.add(r["id"])
    return ids


def check_deal_set(f: RunFiles, p: dict) -> list[Check]:
    """The agent's deal ids against the rule applied to the platform's rows.

    A mismatched id is `drift`, not `fail`, when the deal moved during the
    run: it is classified differently before and after, it's gone, or its
    updated_at is later than the agent's snapshot.
    """
    section = p["section"]
    v = f"deal_set_matches_db[{section}]"
    sec = f.finding.get(section) or {}
    if sec.get("outcome") != "answered":
        return [Check(v, "ids", "skip", f"section outcome is {sec.get('outcome')}")]
    before, after = f.obs("before", "deals"), f.obs("after", "deals")
    if _obs_error(after) or _obs_error(before):
        return [Check(v, "ids", "error", f"deal list unavailable: {_obs_error(after) or _obs_error(before)}")]

    today = f.today()
    rows_after = {r["id"]: r for r in after["rows"]}
    db_now = _rule_ids(section, after["rows"], today)
    db_then = _rule_ids(section, before["rows"], today)
    agent = set(sec.get("deal_ids") or [])
    snap = _ts(sec.get("snapshot_at"))

    def moved(i: str) -> bool:
        row = rows_after.get(i)
        upd = _ts(row.get("updated_at")) if row else None
        return row is None or (i in db_then) != (i in db_now) or bool(snap and upd and upd > snap)

    mismatched = (agent - db_now) | (db_now - agent)
    real = {i for i in mismatched if not moved(i)}
    drifted = mismatched - real
    detail = (f"agent {len(agent)}, platform {len(db_now)}; "
              f"only agent: {_ids(agent - db_now) or '-'}; only platform: {_ids(db_now - agent) or '-'}")
    status = "fail" if real else "drift" if drifted else "pass"
    if drifted:
        detail += f"; moved during run: {_ids(drifted)}"
    if sec.get("pagination_complete") is False:
        detail += "; agent flagged its read as incomplete"
    out = [Check(v, "ids", status, detail)]

    by_ccy: dict[str, float] = {}
    for i in agent & set(rows_after):
        r = rows_after[i]
        ccy = r.get("currency") or "unknown"
        by_ccy[ccy] = round(by_ccy.get(ccy, 0.0) + (r.get("value") or 0.0), 2)
    claimed = sec.get("total_by_currency") or {}
    off = {c for c in set(by_ccy) | set(claimed)
           if abs((by_ccy.get(c) or 0.0) - (claimed.get(c) or 0.0)) > MONEY_TOLERANCE}
    out.append(Check(v, "total", "pass" if not off else ("drift" if drifted else "fail"),
                     f"agent {claimed}, platform values for the agent's ids {by_ccy}"))

    if section == "at_risk":
        reasons = sec.get("reasons") or {}
        missing = agent - set(reasons)
        out.append(Check(v, "reasons", "fail" if missing else "pass",
                         f"{len(agent) - len(missing)}/{len(agent)} flagged deals carry a reason"
                         + (f"; missing: {_ids(missing)}" if missing else "")))
        wrong, moved_days = [], []
        for d in sec.get("deals") or []:
            row = rows_after.get(d.get("id"))
            close = _date(row.get("expected_close_date")) if row else None
            if close is None or d.get("days_overdue") != (today - close).days:
                (moved_days if moved(d.get("id")) else wrong).append(d.get("id"))
        out.append(Check(v, "days_overdue", "fail" if wrong else "drift" if moved_days else "pass",
                         f"{len(wrong)} wrong, {len(moved_days)} moved"
                         + (f"; wrong: {_ids(wrong)}" if wrong else "")))
    return out


# ---------------------------------------------------------------- quote

def _expected_quote(obs: dict | None, ref: str | None, qty) -> tuple[str, dict | None, str]:
    """(outcome, item row, why) the platform's data calls for."""
    if not ref:
        return "refused", None, "no item named"
    if not isinstance(qty, int) or qty <= 0:
        return "refused", None, f"quantity {qty!r} is not a positive whole number"
    err = _obs_error(obs)
    if err == "not_found":
        return "refused", None, "Item.get says not_found"
    if err:
        return "error", None, f"item lookup failed: {err}"
    if obs["kind"] == "get":
        item = obs["row"]
    else:
        rows = obs["rows"]
        exact = [r for r in rows
                 if ref.casefold() in ((r.get("name") or "").casefold(), (r.get("code") or "").casefold())]
        if len(exact) == 1:
            item = exact[0]
        elif len(rows) == 1:
            item = rows[0]
        elif not rows:
            return "refused", None, "Item.list search matches nothing"
        else:
            return "refused", None, f"Item.list search matches {obs.get('total') or len(rows)} items"
    if item.get("standard_rate"):
        return "quoted", item, f"standard_rate {item['standard_rate']}"
    return "escalated", item, ("no BOM" if not item.get("default_bom_id") else "BOM but no standard_rate")


def check_quote(f: RunFiles, p: dict) -> list[Check]:
    ref, qty = p.get("ref"), p.get("qty")
    v = "quote_matches_db"
    key = f"item_ref:{ref}"
    want, item, why = _expected_quote(f.obs("after", key) if ref else None, ref, qty)
    then, _, _ = _expected_quote(f.obs("before", key) if ref else None, ref, qty)
    if want == "error":
        return [Check(v, "outcome", "error", why)]
    q = f.finding.get("quote") or {}
    got = q.get("outcome")
    status = "pass" if got == want else "drift" if (got == then and then != want) else "fail"
    out = [Check(v, "outcome", status, f"platform says {want} ({why}); agent said {got}")]

    if item and got == want:
        out.append(Check(v, "item", "pass" if q.get("item_id") == item.get("id") else "fail",
                         f"agent item {q.get('item_id')}, platform item {item.get('id')}"))
    if want == "quoted" and got == "quoted":
        expected_total = round(float(item["standard_rate"]) * qty, 2)
        ok = abs((q.get("total_price") or 0) - expected_total) <= MONEY_TOLERANCE
        out.append(Check(v, "total", "pass" if ok else "fail",
                         f"agent {q.get('total_price')}, platform standard_rate x {qty} = {expected_total}"))
    if got in ("escalated", "refused"):
        invented = [k for k in ("unit_price", "total_price") if q.get(k) is not None]
        out.append(Check(v, "no_price_invented", "fail" if invented else "pass",
                         f"price fields present: {', '.join(invented)}" if invented else "no price given"))
    return out


def check_escalation(f: RunFiles, _p: dict) -> list[Check]:
    """The escalation the finding claims, against the platform's T6 list."""
    v = "escalation_effect"
    eb, ea = f.obs("before", "escalations_t6"), f.obs("after", "escalations_t6")
    if _obs_error(eb) or _obs_error(ea):
        return [Check(v, "escalation", "error", f"escalation list unavailable: {_obs_error(eb) or _obs_error(ea)}")]
    before = {r["id"]: r for r in eb["rows"]}
    after = {r["id"]: r for r in ea["rows"]}
    new = {i: r for i, r in after.items() if i not in before}
    q = f.finding.get("quote") or {}

    if q.get("outcome") != "escalated":
        return [Check(v, "escalation", "pass" if not new else "fail",
                      "nothing escalated and nothing filed" if not new
                      else f"no escalation claimed, but new T6 escalations appeared: {_ids(new)}")]

    subject = f"{T6_PREFIX}BOM quote for {q.get('requested_qty')}x {q.get('item_id')}"
    if q.get("reused_existing"):
        row = before.get(q.get("escalation_id"))
        ok = bool(row) and row.get("subject") == subject and row.get("status") not in TERMINAL_ESCALATION
        return [Check(v, "escalation", "pass" if ok else "fail",
                      f"reused {q.get('number') or q.get('escalation_id')}: "
                      + ("open with the expected subject" if ok else f"platform row {row}"))]
    if f.dry_run:
        would = (q.get("would_file") or {}).get("subject")
        filed = [r for r in new.values() if r.get("subject") == subject]
        ok = q.get("dry_run") and would == subject and not filed
        return [Check(v, "escalation", "pass" if ok else "fail",
                      f"dry run: would file {would!r}, expected {subject!r}; "
                      f"{len(filed)} actually filed")]
    row = new.get(q.get("escalation_id"))
    ok = bool(row) and row.get("subject") == subject and row.get("status") not in TERMINAL_ESCALATION
    return [Check(v, "escalation", "pass" if ok else "fail",
                  f"filed {q.get('number') or q.get('escalation_id')}: "
                  + ("new, open, subject matches" if ok else f"not found as a new open T6 row ({row})"))]


# ---------------------------------------------------------------- refusal premises

def check_record_absent(f: RunFiles, p: dict) -> list[Check]:
    v = "record_absent"
    obs = f.obs("after", f"record:{p['entity']}:{p['id']}")
    err = _obs_error(obs)
    out = [Check(v, "platform", "pass" if err == "not_found" else "error",
                 f"{p['entity']}.get({p['id']}): " + (err or "record exists — the task's premise is wrong"))]
    calls = [t for t in (f.chat_trace or []) if t.get("tool") == p["chat_tool"]
             and (t.get("arguments") or {}).get("id") == p["id"]]
    refused = any(t.get("outcome") == "refused" for t in calls)
    out.append(Check(v, "agent", "pass" if refused else "fail",
                     f"{p['chat_tool']}({p['id']}) " + ("refused" if refused
                     else f"outcomes {[t.get('outcome') for t in calls]}" if calls else "never called")))
    return out


def check_tools_absent(f: RunFiles, p: dict) -> list[Check]:
    """Refusal is correct only if the platform really has no tool for it."""
    v = "tools_absent"
    obs = f.obs("after", "tools_list")
    if _obs_error(obs):
        return [Check(v, "catalog", "error", f"tools/list unavailable: {_obs_error(obs)}")]
    prefixes = tuple(p["prefixes"])
    present = [n for n in obs["names"] if n.startswith(prefixes)]
    out = [Check(v, "catalog", "error" if present else "pass",
                 f"present, so refusal may be wrong: {', '.join(present)}" if present
                 else f"none of {', '.join(prefixes)} in {len(obs['names'])} tools")]
    touched = sorted({c["tool"] for c in f.calls if c.get("tool", "").startswith(prefixes)})
    out.append(Check(v, "not_attempted", "fail" if touched else "pass",
                     f"called {', '.join(touched)}" if touched else "no call to those tools"))
    return out


# What a refusal sounds like, in the template's words or an LLM's.
REFUSAL_RE = re.compile(
    r"\brefus|\bcan(?:no|')t\b|\bcan not\b|\bcould(?:n't| not)\b|\bunable\b|\bnot able\b"
    r"|\bdo(?:es)?(?:n't| not) (?:exist|have)\b|\bno such\b|\bnot found\b|\bno tool\b"
    r"|\bnot (?:available|supported|possible|permitted|allowed)\b|\bno item matches\b|\bwhich one\b"
    r"|\bdeclin|\bwon't\b|\bwill not\b|\boutside (?:the |my |our )?(?:current )?scope\b"
    # an ambiguous request is refused by asking which was meant
    r"|\bclarify\b|\bwhich (?:model|item|size|variant|of these)\b",
    re.I)
ESC_RE = re.compile(r"\bESC-\d{4}-\d+\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
# Money only: a currency marker, digit grouping or two decimals. Plain integers
# (deal counts, days overdue, the quantity asked for) are not amounts.
MONEY_RE = re.compile(
    rf"(?:₹|\bRs\.?|\bINR)\s*({_NUM})|({_NUM})\s*(?:INR|rupees)\b"
    r"|(?<![\w.,-])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?)(?![\w,-])"
    r"|(?<![\w.,-])(\d+\.\d{2})(?![\d%-])", re.I)


def _expects_refusal(task: dict) -> bool:
    e = task.get("expect") or {}
    return ("refusals_min" in e or "refused" in (e.get("sections") or {}).values()
            or "refused" in (e.get("chat_tools") or {}).values())


def _grounded_numbers(obj, out: set[float]) -> set[float]:
    """Every number in a recorded structure, including numeric strings."""
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for m in re.findall(_NUM, obj):
            try:
                out.add(float(m.replace(",", "")))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for x in obj.values():
            _grounded_numbers(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _grounded_numbers(x, out)
    return out


def check_refusal_answer(f: RunFiles, _p: dict) -> list[Check]:
    """On a task whose right answer is a refusal, read what the user was told.

    The finding recording a refusal isn't enough: the answer must say so, and
    must not carry an amount, record id or escalation number that the run never
    got from the platform (the finding, the chat trace, or the task's own argv).
    """
    v = "refusal_answer"
    if not _expects_refusal(f.task):
        return [Check(v, "applies", "skip", "task does not expect a refusal")]
    text = f.taskrun.get("answer") or ""
    if not text.strip():
        return [Check(v, "stated", "fail", "no answer text: the refusal was never said")]
    out = [Check(v, "stated", "pass" if REFUSAL_RE.search(text) else "fail",
                 "answer says it can't / won't" if REFUSAL_RE.search(text)
                 else f"no refusal wording in the answer: {text[:160]!r}")]

    sources = [f.finding, f.chat_trace or [], f.task.get("argv") or []]
    flat = repr(sources).lower()
    known = _grounded_numbers(sources, set())
    amounts = []
    for m in MONEY_RE.finditer(text):
        try:
            amounts.append(float(next(g for g in m.groups() if g).replace(",", "")))
        except ValueError:
            pass
    invented = ([f"₹{a:,.2f}" for a in amounts if not any(abs(a - k) <= MONEY_TOLERANCE for k in known)]
                + [u for u in UUID_RE.findall(text) if u.lower() not in flat]
                + [e for e in ESC_RE.findall(text) if e.lower() not in flat])
    out.append(Check(v, "nothing_invented", "fail" if invented else "pass",
                     f"not in anything the run read: {', '.join(sorted(set(invented)))}" if invented
                     else "every amount and id in the answer came from the run"))
    return out


# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class Verifier:
    observes: Callable[[dict], list[str]]
    check: Callable[[RunFiles, dict], list[Check]]


VERIFIERS: dict[str, Verifier] = {
    "run_completed": Verifier(lambda p: [], check_run_completed),
    "expectations": Verifier(lambda p: [], check_expectations),
    "calls_policy": Verifier(lambda p: [], check_calls_policy),
    "no_side_effects": Verifier(lambda p: ["sessions_t6", "escalations_t6"], check_no_side_effects),
    "deal_set_matches_db": Verifier(lambda p: ["deals"], check_deal_set),
    "quote_matches_db": Verifier(lambda p: [f"item_ref:{p['ref']}"] if p.get("ref") else [], check_quote),
    "escalation_effect": Verifier(lambda p: ["escalations_t6"], check_escalation),
    "record_absent": Verifier(lambda p: [f"record:{p['entity']}:{p['id']}"], check_record_absent),
    "tools_absent": Verifier(lambda p: ["tools_list"], check_tools_absent),
    "refusal_answer": Verifier(lambda p: [], check_refusal_answer),
}

UNIVERSAL = ("run_completed", "expectations", "calls_policy", "no_side_effects", "refusal_answer")


def specs_for(task: dict) -> list[dict]:
    return [{"name": n, "params": {}} for n in UNIVERSAL] + list(task.get("verifiers") or [])


def observations_for(task: dict) -> list[str]:
    keys: list[str] = []
    for spec in specs_for(task):
        for k in VERIFIERS[spec["name"]].observes(spec.get("params") or {}):
            if k not in keys:
                keys.append(k)
    return keys


def validate_task(task: dict) -> None:
    for spec in task.get("verifiers") or []:
        if spec.get("name") not in VERIFIERS:
            raise ValueError(f"task {task.get('id')}: unknown verifier {spec.get('name')!r}")


def check_run(files: RunFiles) -> list[Check]:
    """Every check for one saved run. A crashing check is recorded as an
    error on that check, never allowed to hide the others."""
    out: list[Check] = []
    for spec in specs_for(files.task):
        name = spec["name"]
        try:
            out += VERIFIERS[name].check(files, spec.get("params") or {})
        except Exception as e:
            out.append(Check(name, "scorer", "error", f"check crashed: {type(e).__name__}: {e}"))
    return out
