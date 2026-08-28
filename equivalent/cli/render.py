"""Plain-text rendering for the ledger CLI.

A person reads this output, so it always shows full detail -- no receipt
policy filtering happens here.
"""
from __future__ import annotations

import json

from equivalent.ledger.acceptance import FINISHED_WORD

from .session import parse_ts


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
                f"  {row['predicateType']:<20} {row['verdict']:<6} {row['claim_id']}"
                f"  (fix and run {row['producing_action']} again)"
            )
        else:
            lines.append(f"  {row['predicateType']:<20} MISSING  (run: {row['producing_action']})")
    if status["accepted"]:
        # The word depends on the phase: an onboarded code is ready for a
        # person to review and promote, an accepted port is ready to merge.
        lines.append(f"{FINISHED_WORD[status['phase']]} on {short(status['tree'])}")
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


# The transcript's timestamps carry milliseconds and the request log's do
# not, so a joined row shows the finer one where it has it.
TIME_WIDTH = len("00:00:00.000")
TEXT_WIDTH = 80

# Which of the two logs a row was read from. The words are the column, so
# a reader does not have to learn a symbol.
SOURCE_LABELS = {"both": "both", "session": "sess", "request": "req"}


def _clock(ts: str) -> str:
    """The time of day, with milliseconds only where the log recorded them.

    The request log writes whole seconds, so a ".000" on one of its rows
    would claim a precision it does not have.
    """
    moment = parse_ts(ts)
    clock = moment.strftime("%H:%M:%S")
    return f"{clock}.{moment.microsecond // 1000:03d}" if "." in ts else clock


def _one_line(text: str) -> str:
    """The first line of what was said, short enough to sit in a column."""
    first = (text or "").strip().splitlines()
    said = first[0] if first else ""
    return said if len(said) <= TEXT_WIDTH else said[: TEXT_WIDTH - 3] + "..."


def _outcome(row) -> str:
    """What the gateway answered this call, in words.

    A row with no request line beside it says so rather than showing
    nothing: a call the gateway never logged is the interesting case, not
    a blank.
    """
    line = row.request
    if line is None:
        if row.local:
            return "(local)"
        if row.who == "status":
            return "(the gateway logs no request line for status)"
        return "(no request line)"
    if line.outcome == "submitted":
        return f"-> submitted tree {short(line.tree)}"
    if line.outcome == "claim":
        verdict = f" {row.verdict}" if row.verdict else ""
        return f"-> claim {line.claim_id}{verdict}" if line.claim_id else "-> claims filed"
    if line.outcome == "duplicate":
        verdict = f" {row.verdict}" if row.verdict else ""
        return f"-> duplicate {line.claim_id}{verdict}" if line.claim_id else "-> duplicate claims"
    if line.outcome == "read":
        # A read of a claim already filed. It is deliberately not worded
        # like "claim" below: nothing was recorded by this call.
        verdict = f" {row.verdict}" if row.verdict else ""
        return f"-> read claim {line.claim_id}{verdict}"
    if line.outcome == "refused":
        missing = ", ".join(item["predicateType"] for item in (line.missing or ()))
        return f"-> refused missing {missing}"
    return f"-> {line.outcome}"


def render_timeline(join) -> str:
    """The two logs as one timeline, oldest first."""
    lines = []
    for row in join.rows:
        detail = _one_line(row.text) if row.text is not None else _outcome(row)
        lines.append(f"{_clock(row.ts):<{TIME_WIDTH}}  {SOURCE_LABELS[row.source]:<4}  {row.who:<20} {detail}".rstrip())
    return "\n".join(lines) + "\n" if lines else ""


def render_session_summary(summary) -> str:
    lines = [
        f"session {summary.session_id}",
        f"  submits              {summary.submits}",
        f"  refusals             {summary.refusals}",
        f"  duplicates           {summary.duplicates}",
        f"  errors               {summary.errors}",
        f"  fail verdicts        {summary.fail_verdicts}",
        f"  trees                {len(summary.trees)}",
        "  claims",
    ]
    if summary.claims_by_predicate:
        for predicate_type in sorted(summary.claims_by_predicate):
            lines.append(f"    {predicate_type:<20} {summary.claims_by_predicate[predicate_type]}")
    else:
        lines.append("    none")
    lines.append(f"  time to acceptance   {summary.time_to_acceptance}")
    lines.append(f"  calls with no request line   {summary.unmatched_calls}")
    lines.append(f"  request lines with no call   {summary.unmatched_requests}")
    return "\n".join(lines) + "\n"
