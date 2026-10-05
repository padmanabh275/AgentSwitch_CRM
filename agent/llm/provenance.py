"""Reject an id argument the model didn't get from anywhere. Zero I/O.

The model only knows a real id by reading it: in the user's question or in
an earlier tool result. Anything else is a guess, and a guess that happens
to be a real id of the wrong thing doesn't fail — file_escalation would
accept another customer's party_id and page a person about the wrong
company. So before a tool runs, each id argument must have been seen, and
seen as the right kind: a party_id must have appeared as a party_id, not as
some deal's own id.

Ids the user typed in the question are accepted for any argument: the user
named them, so they aren't the model's guess.

Same idea as Team 12's payroll planner provenance check, plus the kind
check they left out.
"""
from __future__ import annotations

import re

# (tool, argument) -> where a value must have been seen. "deal.id" is the `id`
# of a record under a key named "deal" (or an element of a list named "deals").
ID_ARGS: dict[tuple[str, str], frozenset[str]] = {
    ("get_deal", "id"): frozenset({"deal_id", "deal_ids", "deal.id", "deals.id"}),
    ("get_lead", "id"): frozenset({"lead_id", "lead_ids", "lead.id", "leads.id"}),
    ("file_escalation", "party_id"): frozenset({"party_id", "party.id", "parties.id"}),
}


def _collect(value, key: str | None, parent: str | None, out: set[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "id" and isinstance(v, str) and parent:
                out.add((f"{parent}.id", v))
            _collect(v, k, k, out)
    elif isinstance(value, list):
        for v in value:
            _collect(v, key, key, out)  # elements belong to the list's key
    elif isinstance(value, str) and key:
        out.add((key, value))


class SeenIds:
    """Every (where, value) pair the run has seen, plus the user's question."""

    def __init__(self, query: str):
        self.query = query
        self.pairs: set[tuple[str, str]] = set()

    def _in_query(self, value: str) -> bool:
        """As a whole token, so a one-character guess doesn't match any question."""
        return re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", self.query) is not None

    def add_result(self, result: dict) -> None:
        # Top-level "id" has no parent, so a refusal echoing the model's own
        # guess back ({"outcome": "refused", "id": <guess>}) never counts.
        _collect(result, None, None, self.pairs)

    def check(self, tool: str, args: dict) -> dict | None:
        """None if every id argument is accounted for, else the error result
        to hand back to the model instead of running the tool."""
        for (t, arg), sources in ID_ARGS.items():
            if t != tool:
                continue
            value = args.get(arg)
            if not isinstance(value, str) or not value.strip():
                continue  # absent optional arg; required ones fail in the tool itself
            value = value.strip()
            if self._in_query(value) or any((s, value) in self.pairs for s in sources):
                continue
            seen_as = sorted(w for w, v in self.pairs if v == value)
            where = (f"it appeared only as {', '.join(seen_as)}, which is not a {arg}"
                     if seen_as else "it did not appear in the question or any earlier result")
            return {"outcome": "error", "error_code": "unseen_id",
                    "reason": (f"{tool}.{arg}={value!r} was not run: {where}. Use a {arg} "
                               f"taken from a result (one of: {', '.join(sorted(sources))}).")}
        return None
