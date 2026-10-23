#!/usr/bin/env python3
"""Do the tests catch a broken agent? Break one line at a time and see.

Copies the repo (without runs/, .venv/ or .git/) into a temporary folder,
plants each bug below in the copy, runs pytest there, and reports whether
any test failed. The working tree is never touched.

Usage (repo root):  uv run python scripts/mutation_probe.py
A bug that "survived" means no test noticed it: write one that does.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (what the bug does, file, exact text, replacement)
BUGS = [
    ("at_risk counts deals due today (<=)", "agent/domain/deals.py", "elif close < today:", "elif close <= today:"),
    ("closing_this_month ignores the year", "agent/domain/deals.py",
     "(close.year, close.month) == (today.year, today.month)", "close.month == today.month"),
    ("today in UTC, not IST", "agent/domain/deals.py",
     "return dt.datetime.now(IST).date()", "return dt.date.today()"),
    ("pagination_complete always True", "agent/domain/deals.py", "total is not None and len(deals) >= total", "True"),
    ("mixed currencies summed into one total", "agent/domain/deals.py",
     "    if len(by_ccy) > 1:\n        return None, by_ccy", "    if False:\n        return None, by_ccy"),
    ("deals not sorted largest first", "agent/domain/deals.py",
     "    matches.sort(key=lambda d: d.get(\"value\") or 0.0, reverse=True)\n\n    total_value, by_ccy = _totals(matches)\n    return {\n        \"outcome\": \"answered\",\n        \"month\"",
     "    total_value, by_ccy = _totals(matches)\n    return {\n        \"outcome\": \"answered\",\n        \"month\""),
    ("MCP error inside HTTP 200 read as success", "agent/transport/mcp_client.py",
     "        if \"error\" in d:", "        if False:"),
    ("MCP isError result read as success", "agent/transport/mcp_client.py",
     "        if result.get(\"isError\"):", "        if False:"),
    ("open escalation never reused (duplicates filed)", "agent/domain/escalation.py",
     "    if existing:\n        return _summary(existing, reused=True)", "    if False:\n        return _summary(existing, reused=True)"),
    ("prose verifier always ok", "agent/llm/verify.py",
     "    ok = not (unsupported_ids or unsupported_escalations or unsupported_amounts or missing)", "    ok = True"),
    ("quote skips the pre-write deal re-read", "agent/domain/quotations.py",
     "    deal, refusal = _reread_deal(client, first, base)", "    deal, refusal = first, None"),
    ("sent quote ordered without the customer accepting", "agent/domain/quote_order.py",
     "        if not customer_accepted:", "        if False:"),
    ("harness calls_policy never fails", "agent/harness/verifiers.py",
     "    if bad:\n        return [Check(\"calls_policy\"", "    if False:\n        return [Check(\"calls_policy\""),
    ("harness deal-set ids never fail", "agent/harness/verifiers.py",
     "    status = \"fail\" if real else \"drift\" if drifted else \"pass\"", "    status = \"pass\""),
    ("harness deal-set total never fails", "agent/harness/verifiers.py",
     "    out.append(Check(v, \"total\", \"pass\" if not off else", "    out.append(Check(v, \"total\", \"pass\" if True else"),
    ("harness refusal_answer passes invented ids/amounts", "agent/harness/verifiers.py",
     "    out.append(Check(v, \"nothing_invented\", \"fail\" if invented else \"pass\",",
     "    out.append(Check(v, \"nothing_invented\", \"pass\","),
    ("harness drift test ignores updated_at", "agent/harness/verifiers.py",
     "or bool(snap and upd and upd > snap)", "or True"),
]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="mutation-probe-"))
    try:
        shutil.copytree(REPO, work, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", ".venv", "runs", "__pycache__", ".pytest_cache"))
        survived = 0
        for name, rel, old, new in BUGS:
            path = work / rel
            original = path.read_text(encoding="utf-8")
            if original.count(old) == 0:
                print(f"  NOT APPLIED  {name}: the line changed; update this probe")
                survived += 1
                continue
            path.write_text(original.replace(old, new), encoding="utf-8")
            failed = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                                    cwd=work, capture_output=True).returncode != 0
            path.write_text(original, encoding="utf-8")
            survived += not failed
            print(f"  {'caught  ' if failed else 'SURVIVED'}  {name}")
        print(f"{len(BUGS) - survived} of {len(BUGS)} planted bugs caught by the tests.")
        return 1 if survived else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
