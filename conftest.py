# The agent uses script-style imports (`from domain import deals`), so put
# agent/ on sys.path for pytest wherever the tests live in this repo.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent"))
