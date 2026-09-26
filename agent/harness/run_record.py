"""TaskRun: the one on-disk record every run produces, saved before
anything scores it.

Ported from designreview/core/harness.py (S18's raw-run-then-score split):
a scorer bug is then fixed by re-scoring saved runs, not by re-running a
live, paid agent. The graph checkpoint sits beside it in the same run
directory and holds the full node data (including the deal snapshot a
verifier can diff against); this record stays light enough to read.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Step:
    """One node or tool call: what ran, how it ended, and why."""
    target: str                 # node id (graph) or tool name (chat)
    kind: str                   # registry name, or "tool" for the chat loop
    status: str
    reason: str | None = None
    error_code: str | None = None
    attempts: int = 1
    added_by: str | None = None
    seconds: float | None = None


@dataclass
class TaskRun:
    run_id: str
    task_id: str                # "pipeline_review" | "chat"
    prompt: str
    dry_run: bool
    session_id: str | None = None
    steps: list[Step] = field(default_factory=list)
    answer: str | None = None
    answer_source: str | None = None   # "llm" | "template"
    finding: dict | None = None
    ended: str = "done"                # done | error
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "taskrun.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path

    @classmethod
    def load(cls, path: Path) -> "TaskRun":
        data = json.loads(path.read_text())
        steps = [Step(**s) for s in data.pop("steps", [])]
        return cls(**data, steps=steps)
