#!/usr/bin/env python3
"""Does the scorer catch a bad run? Plant one fault at a time and see.

Takes saved runs that pass under the current verifiers, copies each one in
memory once per fault that applies to it, plants that fault (a write during
a dry run, a dropped deal, a wrong total, an invented id in the answer ...)
and re-scores the copy. The check the fault targets must then say fail (or
error). If it still passes, the harness is broken, not the agent.

Files only: no network, no agent, no LLM, and nothing is written — saved
runs and their score.json stay as they are. Every verifier a run uses that
no fault exercised is listed at the end, so a gap can't hide.

Usage (from agent/):
    uv run python -m harness.calibrate ../runs/batches/<batch_id> [more batches or run dirs]
"""
from __future__ import annotations

import argparse
import copy
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.run_record import read_json
from harness.score import task_status
from harness.verifiers import (_expects_refusal, _ts, RunFiles, check_run, resolve, specs_for)

INVENTED_UUID = "0badc0de-1234-4abc-8def-0123456789ab"
INVENTED_AMOUNT = "₹9,87,654.32"
CAUGHT = ("fail", "error")


@dataclass(frozen=True)
class Fault:
    name: str
    verifier: str         # the check's verifier; "deal_set_matches_db" matches every [section]
    check: str            # the check's name; "a|b" either one; "prefix:" any name starting so
    plant: Callable[[RunFiles], RunFiles | None]   # None: the fault doesn't apply to this run


def _clone(f: RunFiles) -> RunFiles:
    return copy.deepcopy(f)


def _verifiers(f: RunFiles) -> set[str]:
    return {s["name"] for s in specs_for(f.task)}


def _params(f: RunFiles, name: str) -> list[dict]:
    return [s.get("params") or {} for s in specs_for(f.task) if s["name"] == name]


def _obs_row(f: RunFiles, phase: str, key: str) -> dict | None:
    """The `row` of an observation, for editing in place (the clone's)."""
    src = f.before if phase == "before" else f.after
    o = (src.get("observations") or {}).get(resolve(key, f.fixture))
    return o.get("row") if isinstance(o, dict) and isinstance(o.get("row"), dict) else None


def _add_call(f: RunFiles, tool: str, args: dict | None = None) -> None:
    f.calls.append({"seq": len(f.calls) + 1, "tool": tool, "args": args or {}, "ok": True,
                    "error_code": None, "result_id": None, "seconds": 0.1})


def _answered(f: RunFiles, section: str) -> dict | None:
    sec = f.finding.get(section)
    return sec if isinstance(sec, dict) and sec.get("outcome") == "answered" else None


# ---------------------------------------------------------------- universal checks

def run_crashed(f):
    m = _clone(f)
    m.taskrun["ended"] = "running"
    return m


def write_in_dry_run(f):
    if not f.dry_run:
        return None
    m = _clone(f)
    _add_call(m, "AgentEscalation.create", {"subject": "T6-planted"})
    return m


def disallowed_write(f):
    if f.dry_run:
        return None
    m = _clone(f)
    _add_call(m, "Party.delete", {"id": INVENTED_UUID})
    return m


def session_left_by_dry_run(f):
    o = (f.after.get("observations") or {}).get("sessions_t6")
    if not f.dry_run or not isinstance(o, dict) or o.get("error_code"):
        return None
    m = _clone(f)
    m.after["observations"]["sessions_t6"]["total"] = (o.get("total") or 0) + 1
    return m


def escalation_left_by_dry_run(f):
    o = (f.after.get("observations") or {}).get("escalations_t6")
    if not f.dry_run or not isinstance(o, dict) or o.get("error_code"):
        return None
    m = _clone(f)
    m.after["observations"]["escalations_t6"]["rows"].append(
        {"id": INVENTED_UUID, "subject": "T6-planted", "status": "open"})
    return m


def section_outcome_flipped(f):
    pins = (f.task.get("expect") or {}).get("sections") or {}
    if not pins:
        return None
    m = _clone(f)
    for name, want in pins.items():
        sec = m.finding.setdefault(name, {})
        sec["outcome"] = "error" if want != "error" else "answered"
    return m


# ---------------------------------------------------------------- deal lists

def _deal_set_sections(f):
    return [p["section"] for p in _params(f, "deal_set_matches_db") if _answered(f, p["section"])]


def dropped_deal(f):
    """Remove a deal the platform agrees on and that didn't move during the run."""
    rows = {r["id"]: r for r in ((f.after.get("observations") or {}).get("deals") or {}).get("rows") or []}
    m = _clone(f)
    for section in _deal_set_sections(f):
        sec = m.finding[section]
        snap = _ts(sec.get("snapshot_at"))
        for i in sec.get("deal_ids") or []:
            upd = _ts((rows.get(i) or {}).get("updated_at"))
            if i in rows and snap and upd and upd <= snap:
                sec["deal_ids"] = [d for d in sec["deal_ids"] if d != i]
                return m
    return None


def wrong_total(f):
    sections = [s for s in _deal_set_sections(f) if f.finding[s].get("total_by_currency")]
    if not sections:
        return None
    m = _clone(f)
    for s in sections:
        totals = m.finding[s]["total_by_currency"]
        for ccy in totals:
            totals[ccy] = (totals[ccy] or 0.0) + 1000.0
    return m


def wrong_days_overdue(f):
    if "at_risk" not in _deal_set_sections(f) or not f.finding["at_risk"].get("deals"):
        return None
    m = _clone(f)
    d = m.finding["at_risk"]["deals"][0]
    d["days_overdue"] = (d.get("days_overdue") or 0) + 3
    return m


def missing_reason(f):
    if "at_risk" not in _deal_set_sections(f) or not f.finding["at_risk"].get("reasons"):
        return None
    m = _clone(f)
    reasons = m.finding["at_risk"]["reasons"]
    reasons.pop(next(iter(reasons)))
    return m


# ---------------------------------------------------------------- quote and escalation

def _quote(f):
    q = f.finding.get("quote")
    return q if isinstance(q, dict) else None


def price_on_unpriced_quote(f):
    q = _quote(f)
    if "quote_matches_db" not in _verifiers(f) or not q or q.get("outcome") not in ("escalated", "refused", "unpriced"):
        return None
    m = _clone(f)
    m.finding["quote"]["total_price"] = 123456.0
    return m


def quote_outcome_changed(f):
    q = _quote(f)
    if "quote_matches_db" not in _verifiers(f) or not q or not q.get("outcome"):
        return None
    m = _clone(f)
    m.finding["quote"]["outcome"] = "answered" if q["outcome"] != "answered" else "refused"
    return m


def wrong_escalation_subject(f):
    q = _quote(f)
    if ("escalation_effect" not in _verifiers(f) or not q or q.get("outcome") != "escalated"
            or not f.dry_run or not (q.get("would_file") or {}).get("subject")):
        return None
    m = _clone(f)
    m.finding["quote"]["would_file"]["subject"] = "T6-BOM quote for 1x someone-else"
    return m


# ---------------------------------------------------------------- what the user is told

def _refusal_task(f):
    return "refusal_answer" in _verifiers(f) and _expects_refusal(f.task) and (f.taskrun.get("answer") or "").strip()


def _with_answer(f, text):
    m = _clone(f)
    m.taskrun["answer"] = text
    return m


def refusal_not_said(f):
    return _with_answer(f, "Here are the details you asked for.") if _refusal_task(f) else None


def invented_id_in_answer(f):
    return _with_answer(f, f.taskrun["answer"] + f" Reference {INVENTED_UUID}.") if _refusal_task(f) else None


def invented_amount_in_answer(f):
    return _with_answer(f, f.taskrun["answer"] + f" It would cost {INVENTED_AMOUNT}.") if _refusal_task(f) else None


def false_escalation_claim(f):
    if not (_refusal_task(f) and f.dry_run):
        return None
    return _with_answer(f, f.taskrun["answer"] + " I have escalated this to the pricing team.")


# ---------------------------------------------------------------- refusal premises

def chat_tool_not_refused(f):
    params = _params(f, "record_absent")
    if not params or not f.chat_trace:
        return None
    m = _clone(f)
    hit = False
    for p in params:
        for t in m.chat_trace:
            if t.get("tool") == p["chat_tool"] and (t.get("arguments") or {}).get("id") == p["id"]:
                t["outcome"] = "answered"
                hit = True
    return m if hit else None


def absent_tool_called(f):
    params = _params(f, "tools_absent")
    if not params:
        return None
    m = _clone(f)
    _add_call(m, params[0]["prefixes"][0] + "planted")
    return m


def permitted_tool_used(f):
    params = _params(f, "action_not_taken")
    if not params:
        return None
    m = _clone(f)
    _add_call(m, params[0]["tool_prefix"] + "planted", {"id": params[0]["deal_id"]})
    return m


# ---------------------------------------------------------------- write tasks

def bypass_transition(f):
    """A quoting run that also converted the quote straight to an order."""
    if not ({"quotation_written", "stale_deal_guard"} & _verifiers(f)):
        return None
    m = _clone(f)
    _add_call(m, "Quotation.convert_to_order.sent.converted", {"id": INVENTED_UUID})
    return m


def deal_write_on_untouched_deal(f):
    if not ({"deal_not_won", "deal_not_moved"} & _verifiers(f)):
        return None
    m = _clone(f)
    _add_call(m, "Deal.qualify.new.qualification", {"id": (f.fixture or {}).get("deal_id")})
    return m


def deal_moved_on_untouched_deal(f):
    if not ({"deal_not_won", "deal_not_moved"} & _verifiers(f)) or not _obs_row(f, "after", "deal_state:{deal_id}"):
        return None
    m = _clone(f)
    _obs_row(m, "after", "deal_state:{deal_id}")["stage"] = "qualification"
    return m


def lead_write_on_untouched_lead(f):
    if "lead_unchanged" not in _verifiers(f):
        return None
    m = _clone(f)
    _add_call(m, "Lead.qualify.contacted.qualified", {"id": (f.fixture or {}).get("lead_id")})
    return m


def lead_moved_on_untouched_lead(f):
    if "lead_unchanged" not in _verifiers(f) or not _obs_row(f, "after", "lead_state:{lead_id}"):
        return None
    m = _clone(f)
    _obs_row(m, "after", "lead_state:{lead_id}")["status"] = "contacted"
    return m


def deal_left_where_it_was(f):
    if "deal_advanced" not in _verifiers(f) or not _obs_row(f, "before", "deal_state:{deal_id}"):
        return None
    m = _clone(f)
    before = _obs_row(m, "before", "deal_state:{deal_id}")
    _obs_row(m, "after", "deal_state:{deal_id}").update(stage=before.get("stage"), updated_at=before.get("updated_at"))
    return m


def extra_step_on_deal(f):
    if "deal_advanced" not in _verifiers(f):
        return None
    m = _clone(f)
    _add_call(m, "Deal.negotiate.proposal.negotiation", {"id": (f.fixture or {}).get("deal_id")})
    return m


def lead_left_where_it_was(f):
    if "lead_advanced" not in _verifiers(f) or not _obs_row(f, "before", "lead_state:{lead_id}"):
        return None
    m = _clone(f)
    before = _obs_row(m, "before", "lead_state:{lead_id}")
    _obs_row(m, "after", "lead_state:{lead_id}").update(status=before.get("status"), updated_at=before.get("updated_at"))
    return m


def extra_step_on_lead(f):
    if "lead_advanced" not in _verifiers(f):
        return None
    m = _clone(f)
    _add_call(m, "Lead.make.Deal", {"id": (f.fixture or {}).get("lead_id")})
    return m


def second_deal_from_lead(f):
    o = (f.after.get("observations") or {}).get(resolve("lead_deals:{lead_id}", f.fixture))
    if not any(p.get("makes_deal") or p.get("keeps_deal") for p in _params(f, "lead_advanced")) \
            or not isinstance(o, dict) or not isinstance(o.get("rows"), list):
        return None
    m = _clone(f)
    m.after["observations"][resolve("lead_deals:{lead_id}", f.fixture)]["rows"].append({"id": INVENTED_UUID})
    return m


FAULTS = [
    Fault("run_crashed", "run_completed", "ended", run_crashed),
    Fault("write_in_dry_run", "calls_policy", "tools", write_in_dry_run),
    Fault("disallowed_write", "calls_policy", "tools", disallowed_write),
    Fault("session_left_by_dry_run", "no_side_effects", "sessions", session_left_by_dry_run),
    Fault("escalation_left_by_dry_run", "no_side_effects", "escalations", escalation_left_by_dry_run),
    Fault("section_outcome_flipped", "expectations", "section:", section_outcome_flipped),
    Fault("dropped_deal", "deal_set_matches_db", "ids", dropped_deal),
    Fault("wrong_total", "deal_set_matches_db", "total", wrong_total),
    Fault("wrong_days_overdue", "deal_set_matches_db", "days_overdue", wrong_days_overdue),
    Fault("missing_reason", "deal_set_matches_db", "reasons", missing_reason),
    Fault("price_on_unpriced_quote", "quote_matches_db", "no_price_invented", price_on_unpriced_quote),
    Fault("quote_outcome_changed", "quote_matches_db", "outcome", quote_outcome_changed),
    Fault("wrong_escalation_subject", "escalation_effect", "escalation", wrong_escalation_subject),
    Fault("refusal_not_said", "refusal_answer", "stated", refusal_not_said),
    Fault("invented_id_in_answer", "refusal_answer", "nothing_invented", invented_id_in_answer),
    Fault("invented_amount_in_answer", "refusal_answer", "nothing_invented", invented_amount_in_answer),
    Fault("false_escalation_claim", "refusal_answer", "no_false_escalation", false_escalation_claim),
    Fault("chat_tool_not_refused", "record_absent", "agent", chat_tool_not_refused),
    Fault("absent_tool_called", "tools_absent", "not_attempted", absent_tool_called),
    Fault("permitted_tool_used", "action_not_taken", "not_called", permitted_tool_used),
    Fault("bypass_transition", "*", "no_bypass", bypass_transition),
    Fault("deal_write_on_untouched_deal", "*", "no_deal_write", deal_write_on_untouched_deal),
    Fault("deal_moved_on_untouched_deal", "*", "deal_untouched", deal_moved_on_untouched_deal),
    Fault("lead_write_on_untouched_lead", "lead_unchanged", "no_funnel_write", lead_write_on_untouched_lead),
    Fault("lead_moved_on_untouched_lead", "lead_unchanged", "lead_untouched", lead_moved_on_untouched_lead),
    Fault("deal_left_where_it_was", "deal_advanced", "deal_moved", deal_left_where_it_was),
    Fault("extra_step_on_deal", "deal_advanced", "one_step", extra_step_on_deal),
    Fault("lead_left_where_it_was", "lead_advanced", "lead_moved", lead_left_where_it_was),
    Fault("extra_step_on_lead", "lead_advanced", "one_step", extra_step_on_lead),
    Fault("second_deal_from_lead", "lead_advanced", "one_deal|deal_linked", second_deal_from_lead),
]


def _targets(fault: Fault, check) -> bool:
    verifier_ok = (fault.verifier == "*" or check.verifier == fault.verifier
                   or check.verifier.startswith(fault.verifier + "["))
    name_ok = (check.name.startswith(fault.check) if fault.check.endswith(":")
               else check.name in fault.check.split("|"))
    return verifier_ok and name_ok


def caught(fault: Fault, mutant: RunFiles) -> tuple[set[str], str]:
    """The verifiers whose targeted check flagged the planted fault (empty:
    not caught), and what the check said."""
    hits = [c for c in check_run(mutant) if _targets(fault, c)]
    if not hits:
        return set(), "the targeted check did not run"
    flagged = [c for c in hits if c.status in CAUGHT]
    shown = flagged[0] if flagged else hits[0]
    return ({c.verifier.split("[")[0] for c in flagged},
            f"{shown.verifier}/{shown.name} {shown.status}: {shown.detail[:120]}")


def scorable(run_dir: Path) -> bool:
    """A run left a record: its taskrun.json, or a task in steps its steps.json."""
    return (run_dir / "taskrun.json").exists() or (run_dir / "steps.json").exists()


def run_dirs(paths: list[Path]) -> list[Path]:
    out = []
    for path in paths:
        if (path / "manifest.json").exists():
            runs_dir = path.parent.parent   # runs/batches/<batch_id> -> runs/
            out += [runs_dir / e["run_id"] for e in read_json(path / "manifest.json").get("runs", [])
                    if e.get("status") == "ran"]
        else:
            out.append(path)
    return [d for d in out if scorable(d)]


def calibrate(dirs: list[Path]) -> dict:
    planted = defaultdict(int)
    missed: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str]] = []
    used: set[str] = set()
    exercised: set[str] = set()
    for run_dir in dirs:
        files = RunFiles.load(run_dir)
        if files.task.get("steps"):
            skipped.append((run_dir.name, "a task in steps: its steps are scored as their own runs"))
            continue
        status = task_status(check_run(files))
        if status != "pass":
            skipped.append((run_dir.name, f"scores {status} under the current verifiers"))
            continue
        used |= _verifiers(files)
        for fault in FAULTS:
            mutant = fault.plant(files)
            if mutant is None:
                continue
            planted[fault.name] += 1
            by, said = caught(fault, mutant)
            if by:
                exercised |= by
            else:
                missed.append((run_dir.name, fault.name, said))
    return {"planted": dict(planted), "missed": missed, "skipped": skipped,
            "unexercised": sorted(used - exercised)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Plant faults in saved passing runs; the scorer must catch each.")
    ap.add_argument("paths", nargs="+", type=Path, help="batch dirs (runs/batches/<id>) or run dirs")
    args = ap.parse_args()
    result = calibrate(run_dirs(args.paths))
    total = sum(result["planted"].values())
    for name, n in sorted(result["planted"].items()):
        miss = sum(1 for _, f, _ in result["missed"] if f == name)
        print(f"  {'MISSED' if miss else 'ok':6} {name:30} {n - miss}/{n} caught")
    for run, fault, said in result["missed"]:
        print(f"  MISSED {fault} in {run}: {said}")
    for run, why in result["skipped"]:
        print(f"  skipped {run}: {why}")
    if result["unexercised"]:
        print(f"  no fault exercised: {', '.join(result['unexercised'])}")
    print(f"{total - len(result['missed'])} of {total} planted faults caught.")
    return 1 if result["missed"] or not total else 0


if __name__ == "__main__":
    raise SystemExit(main())
