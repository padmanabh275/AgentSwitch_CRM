"""Paths and .env loading shared by run.py and anything else that needs creds."""
from __future__ import annotations

import os
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_DIR = AGENT_DIR.parent
DEFAULT_ENV_PATH = AGENT_DIR / ".env"
REPO_ENV_PATH = REPO_DIR / ".env"
RUNS_DIR = REPO_DIR / "runs"   # gitignored: runs/<run_id>/{taskrun,graph}.json


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict:
    """os.environ, then agent/.env, then the repo's .env — each only fills
    in what an earlier source didn't set."""
    env = dict(os.environ)
    for p in (path, REPO_ENV_PATH):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    return env
