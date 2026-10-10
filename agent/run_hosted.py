#!/usr/bin/env python3
"""Launcher for the hosted harness run, callable from the repo root without
a shell: `python agent/run_hosted.py ...`. Running a file puts its own
directory (agent/) on sys.path, which the script-style imports need. All
the work is in harness/hosted.py; arguments pass straight through.
"""
from harness.hosted import main

if __name__ == "__main__":
    raise SystemExit(main())
