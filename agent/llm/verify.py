"""verify_claims(): does the prose say only what the finding says, and enough of it?

Two kinds of problem, both checked deterministically (no LLM):

- **unsupported**: something stated that isn't in the finding — a record id,
  an escalation number, or a money amount that matches no number in it
  (within ₹1, so "264,000" for 264000.0 is fine but "₹6.8 million" isn't).
- **missing**: a fact the answer must carry but left out — a section total,
  the count of open deals with no close date, that a quote was escalated
  (or, in a dry run, that nothing was filed), that a list may be partial.

Money detection is deliberately narrow: an amount needs a currency marker
(₹, Rs, INR) or digit grouping (1,234 / 1,23,456) or two decimals. Plain
small integers — deal counts, days overdue, quantities — are not money.
"""
from __future__ import annotations

import re

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
ESC_RE = re.compile(r"\bESC-\d{4}-\d+\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
MONEY_RE = re.compile(
    rf"(?:₹|\bRs\.?|\bINR)\s*({_NUM})"                   # ₹2,64,000  Rs. 5799  INR 100
    rf"|({_NUM})\s*(?:INR|rupees)\b"                     # 264,000 INR
    r"|(?<![\w.,-])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?)(?![\w,-])"   # 6,817,734.14 / 68,17,734.14
    r"|(?<![\w.,-])(\d+\.\d{2})(?![\d%-])",              # 5799.00
    re.I,
)
TOLERANCE = 1.0   # ₹1: allows rounding to whole rupees, nothing looser


def _numbers(obj, out: set[float]) -> set[float]:
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers(v, out)
    return out


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _mentions_amount(text: str, value: float) -> bool:
    return any(abs(v - value) <= TOLERANCE for v in money_in(text))


def money_in(text: str) -> list[float]:
    found = []
    for m in MONEY_RE.finditer(text):
        v = _to_float(next(g for g in m.groups() if g))
        if v is not None:
            found.append(v)
    return found


def verify_claims(text: str, finding: dict) -> dict:
    flat = repr(finding).lower()
    known = _numbers(finding, set())

    unsupported_ids = sorted({u for u in UUID_RE.findall(text) if u.lower() not in flat})
    unsupported_escalations = sorted({e for e in ESC_RE.findall(text) if e.lower() not in flat})
    unsupported_amounts = sorted({v for v in money_in(text)
                                  if not any(abs(v - k) <= TOLERANCE for k in known)})

    missing: list[str] = []
    for name in ("closing_this_month", "at_risk"):
        s = finding.get(name) or {}
        if s.get("outcome") != "answered":
            continue
        total = s.get("total_value")
        if s.get("deal_ids") and total and not _mentions_amount(text, total):
            missing.append(f"{name} total {total:,.2f}")
        if s.get("pagination_complete") is False and not re.search(r"incomplete|partial", text, re.I):
            missing.append(f"{name} may be incomplete (pagination_complete is false)")
    risk = finding.get("at_risk") or {}
    n_undated = len(risk.get("open_without_close_date_ids") or [])
    if risk.get("outcome") == "answered" and n_undated and not re.search(rf"\b{n_undated}\b", text):
        missing.append(f"{n_undated} open deals have no expected close date")

    quote = finding.get("quote") or {}
    if quote.get("outcome") == "escalated":
        if quote.get("dry_run"):
            if not re.search(r"dry[- ]run|not (?:actually )?filed|nothing (?:was )?filed", text, re.I):
                missing.append("dry run: no escalation was actually filed")
        elif quote.get("number") and quote["number"] not in text:
            missing.append(f"escalation number {quote['number']}")
        elif not re.search(r"escalat", text, re.I):
            missing.append("the quote was escalated")
    elif quote.get("outcome") == "quoted" and not _mentions_amount(text, quote.get("total_price") or 0):
        missing.append(f"quote total {quote.get('total_price'):,.2f}")

    ok = not (unsupported_ids or unsupported_escalations or unsupported_amounts or missing)
    return {"ok": ok, "unsupported_ids": unsupported_ids,
            "unsupported_escalations": unsupported_escalations,
            "unsupported_amounts": unsupported_amounts, "missing": missing}


def feedback(check: dict) -> str:
    """The correction sent back to the LLM for its one retry."""
    lines = ["Your answer doesn't match the FINDING. Rewrite the whole answer for the "
             "user from scratch, and don't mention that it was corrected."]
    if check["unsupported_ids"] or check["unsupported_escalations"]:
        lines.append("- These references are not in the FINDING; remove them: "
                     + ", ".join(check["unsupported_ids"] + check["unsupported_escalations"]))
    if check["unsupported_amounts"]:
        lines.append("- These amounts are not in the FINDING: "
                     + ", ".join(f"{a:,.2f}" for a in check["unsupported_amounts"])
                     + ". Use only amounts from the FINDING, in full digits with ₹ "
                       "(e.g. ₹2,64,000.00) — no lakh/crore/million, no new totals.")
    if check["missing"]:
        lines.append("- You must also state: " + "; ".join(check["missing"]) + ".")
    return "\n".join(lines)
