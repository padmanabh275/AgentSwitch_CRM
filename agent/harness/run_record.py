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
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace, so a reader (or a crash) never
    sees half a file. Retries briefly: OneDrive/AV can hold the target open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def write_json(path: Path, obj) -> Path:
    atomic_write_text(path, json.dumps(obj, indent=2, default=str))
    return path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
    task_id: str                # "pipeline_review" | "chat" (the path that answered)
    prompt: str
    dry_run: bool
    today: str | None = None     # set only when pinned with --today
    harness_task: str | None = None   # tasks.json id, when the harness launched this run
    intent: dict | None = None   # parse_request's classification, for a free-text question
    session_id: str | None = None
    steps: list[Step] = field(default_factory=list)
    answer: str | None = None
    answer_source: str | None = None   # "llm" | "template"
    finding: dict | None = None
    ended: str = "done"                # running | done | error ("running" left behind = crashed)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, run_dir: Path) -> Path:
        return write_json(run_dir / "taskrun.json", self.to_dict())

    @classmethod
    def load(cls, path: Path) -> "TaskRun":
        data = read_json(path)
        steps = [Step(**s) for s in data.pop("steps", [])]
        return cls(**data, steps=steps)
