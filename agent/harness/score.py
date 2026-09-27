#!/usr/bin/env python3
"""Score saved runs. Reads files only — no network, no agent, no LLM.

Give it a batch directory (runs/batches/<batch_id>, via its manifest) or
run directories. Each run gets runs/<run_id>/score.json; a batch also gets
report.md beside its manifest. Re-running it over an old batch re-scores
it with the current verifiers: the saved evidence doesn't change.

Task status: fail if any check failed or couldn't establish ground truth
(error); drift if the only mismatches are deals that moved during the run;
otherwise pass.

Usage (from agent/):
    uv run python -m harness.score runs/batches/<batch_id>
    uv run python -m harness.score runs/<run_id> [runs/<run_id> ...]
"""
from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter
from pathlib import Path

from harness.run_record import read_json, write_json
from harness.verifiers import IST, Check, RunFiles, check_run

SCORER_VERSION = "t6-score-v1"


def task_status(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    if statuses & {"fail", "error"}:
        return "fail"
    if "drift" in statuses:
        return "drift"
    return "pass"


def score_run(run_dir: Path) -> dict:
    files = RunFiles.load(run_dir)
    checks = check_run(files)
    score = {
        "scorer": SCORER_VERSION,
        "scored_at": dt.datetime.now(IST).isoformat(),
        "run_id": files.taskrun.get("run_id"),
        "task_id": files.task.get("id"),
        "dry_run": files.dry_run,
        "status": task_status(checks),
        "counts": dict(Counter(c.status for c in checks)),
        "checks": [c.to_dict() for c in checks],
    }
    write_json(run_dir / "score.json", score)
    return score


def _row(task_id: str, status: str, note: str) -> str:
    return f"| `{task_id}` | {status} | {note} |"


def write_report(batch_dir: Path, manifest: dict, results: list[tuple[dict, dict | None]]) -> Path:
    totals = Counter((s["status"] if s else e.get("status", "missing")) for e, s in results)
    lines = [
        f"# Harness batch {manifest.get('batch_id')}",
        "",
        f"- Mode: {'write' if manifest.get('write') else 'dry run'}; today pinned to "
        f"{manifest.get('today')}; LLM gateway {'up' if manifest.get('llm_gateway_up') else 'down'}",
        f"- Scored {dt.datetime.now(IST).isoformat()} by {SCORER_VERSION}",
        "- Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())),
        "",
        "| Task | Status | Checks |",
        "|---|---|---|",
    ]
    for entry, score in results:
        if score is None:
            lines.append(_row(entry["task_id"], entry.get("status", "missing"), entry.get("reason", "")))
        else:
            counts = ", ".join(f"{k} {v}" for k, v in sorted(score["counts"].items()))
            lines.append(_row(entry["task_id"], score["status"], counts))
    problems = [(e, s) for e, s in results if s and s["status"] != "pass"]
    if problems:
        lines += ["", "## Checks that did not pass", ""]
        for entry, score in problems:
            lines.append(f"### `{entry['task_id']}` ({score['status']})")
            lines.append("")
            for c in score["checks"]:
                if c["status"] not in ("pass", "skip"):
                    lines.append(f"- **{c['status']}** `{c['verifier']}` / {c['name']}: {c['detail']}")
            lines.append("")
    path = batch_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def score_batch(batch_dir: Path) -> tuple[Path, list[tuple[dict, dict | None]]]:
    manifest = read_json(batch_dir / "manifest.json")
    results = []
    runs_dir = batch_dir.parent.parent   # runs/batches/<batch_id> -> runs/
    for entry in manifest.get("runs", []):
        run_dir = runs_dir / entry["run_id"]
        if entry.get("status") != "ran":
            results.append((entry, None))
            continue
        if not (run_dir / "taskrun.json").exists():
            results.append(({**entry, "status": "fail", "reason": "no taskrun.json was written"}, None))
            continue
        results.append((entry, score_run(run_dir)))
    return write_report(batch_dir, manifest, results), results


def main() -> int:
    ap = argparse.ArgumentParser(description="Score saved harness runs (files only).")
    ap.add_argument("paths", nargs="+", type=Path, help="a batch dir, or run dirs")
    args = ap.parse_args()

    failed = False
    for path in args.paths:
        if (path / "manifest.json").exists():
            report, results = score_batch(path)
            for entry, score in results:
                status = score["status"] if score else entry.get("status", "missing")
                failed |= status == "fail"
                print(f"  {status:8} {entry['task_id']}")
            print(f"[report: {report}]")
        elif (path / "taskrun.json").exists():
            score = score_run(path)
            failed |= score["status"] == "fail"
            print(f"  {score['status']:8} {score['task_id']}  [{path / 'score.json'}]")
        else:
            print(f"  skipped  {path}: no manifest.json or taskrun.json")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
