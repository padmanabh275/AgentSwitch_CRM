"""NodeOutcome: what every graph node ends with.

A refusal or an escalation is a *result*, not a failure — the graph keeps
going and the finding reports it. Only an unexpected exception becomes
`error`, and it keeps the MCP error code so the run record says why.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

ANSWERED = "answered"
REFUSED = "refused"
ESCALATED = "escalated"
DEFERRED = "deferred"   # waiting on something outside this run (Phase 3: escalation replies)
ERROR = "error"
SKIPPED = "skipped"     # a dependency didn't answer, so this node never ran
STATUSES = (ANSWERED, REFUSED, ESCALATED, DEFERRED, ERROR, SKIPPED)

# Domain functions return dicts with their own `outcome` field (the finding
# schema's per-section vocabulary). This maps it onto graph status. "quoted"
# and "unpriced" both mean "the node did its job" — whether an unpriced item
# needs escalating is the planner's call, not the node's.
_DOMAIN_TO_STATUS = {
    "answered": ANSWERED,
    "quoted": ANSWERED,
    "unpriced": ANSWERED,
    "refused": REFUSED,
    "escalated": ESCALATED,
    "deferred": DEFERRED,
    "error": ERROR,
}


@dataclass
class NodeOutcome:
    status: str
    data: dict = field(default_factory=dict)
    reason: str | None = None
    error_code: str | None = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown node status {self.status!r}")

    @classmethod
    def from_domain(cls, result: dict) -> "NodeOutcome":
        domain_outcome = result.get("outcome")
        status = _DOMAIN_TO_STATUS.get(domain_outcome)
        if status is None:
            raise ValueError(f"domain result has unmapped outcome {domain_outcome!r}")
        return cls(status=status, data=result, reason=result.get("reason"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NodeOutcome":
        return cls(**d)
