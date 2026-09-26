"""Paths and .env loading shared by run.py and anything else that needs creds."""
from __future__ import annotations

import os
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_DIR = AGENT_DIR.parent
DEFAULT_ENV_PATH = AGENT_DIR / ".env"
RUNS_DIR = REPO_DIR / "runs"   # gitignored: runs/<run_id>/{taskrun,graph}.json


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict:
    """os.environ, with .env filling in anything not already set."""
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    return env
