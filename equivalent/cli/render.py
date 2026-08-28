"""Plain-text rendering for the ledger CLI.

A person reads this output, so it always shows full detail -- no receipt
policy filtering happens here.
"""
from __future__ import annotations

import json


def short(sha256) -> str:
    return sha256[:12] if sha256 else "none"


def render_status(status: dict, region: str) -> str:
    lines = [f"region {region}   tree {short(status['tree'])} (frozen {short(status['frozen'])})"]
    for row in status["rows"]:
        if row["status"] == "present":
            lines.append(f"  {row['predicateType']:<20} {row['verdict']:<6} {row['claim_id']}")
        elif "claim_id" in row:
            # The latest claim exists but did not pass; that is still an
            # unmet requirement, shown with the failing claim's id.
            lines.append(
                f"  {row['predicateType']:<20} {row['verdict']:<6} {row['claim_id']}  (run again: {row['producing_action']})"
            )
        else:
            lines.append(f"  {row['predicateType']:<20} MISSING  (run: {row['producing_action']})")
    if status["accepted"]:
        lines.append(f"ACCEPTED on {short(status['tree'])}")
    return "\n".join(lines) + "\n"


def render_history(history: list) -> str:
    lines = []
    for entry in history:
        lines.append(f"tree {short(entry['tree'])}")
        for c in entry["claims"]:
            lines.append(f"  {c['predicateType']:<20} {c['verdict']:<6} {c['claim_id']}")
    return "\n".join(lines) + "\n"


def render_claim(claim) -> str:
    return json.dumps(claim.to_dict(), indent=2, sort_keys=True) + "\n"


def render_requests(requests: list) -> str:
    lines = []
    for r in requests:
        line = f"{r.ts}  {r.endpoint:<8} {r.action:<20} {r.outcome}"
        if r.claim_id:
            line += f"  {r.claim_id}"
        if r.missing:
            line += f"  missing={list(r.missing)}"
        lines.append(line)
    return "\n".join(lines) + "\n"
