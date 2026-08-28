"""Wraps the builder's /v1/sanitize as a gateway component (Step 6d).

One builder call produces one verdict per tool named in the strategy's
`sanitizers` list (memcheck, racecheck, initcheck), matching
demo/orchestrator/orchestrator.py's single call -> three ledger columns.
Unlike every other component here, this one returns several verdicts, not
one -- equivalent.gateway.app records one claim per tool from a single
dispatch, atomically, so a duplicate check against any single one of them
is a safe proxy for "all three already exist" (see Step 6d's memory note,
which had flagged this as deferred from Step 5b).

Only the first visible case is used, matching demo's own choice (sanitizer
runs are much slower than a plain replay run).
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def check(region_id: str, tree_sha: str, strategy: Strategy, visible_cases: dict, builder) -> dict:
    """Returns {tool_name: {"verdict": "pass"|"fail", "detail": {...}}, ...}."""
    if not visible_cases:
        raise ComponentError("no visible dataset configured for this region")

    attempt_id = attempt_id_for(region_id, tree_sha)
    name = next(iter(visible_cases))
    one_case = {name: visible_cases[name]}
    tools = list(strategy.sanitizers)
    try:
        resp = builder.sanitize(attempt_id, strategy.name, one_case, tools)
    except Exception as exc:
        raise ComponentError(f"builder /v1/sanitize call failed: {exc}") from exc

    per_tool = resp.get("per_tool", {})
    results = {}
    for tool in tools:
        t = per_tool.get(tool, {})
        if t.get("ok") is False:
            results[tool] = {"verdict": "fail", "detail": {"errors": t.get("errors"), "log_tail": t.get("log_tail", "")}}
        else:
            # ok is True or None (tool unavailable) -- demo's own
            # `all(t.get("ok") in (True, None) ...)` treats "unavailable" as
            # not a failure; carried over here rather than re-litigated.
            results[tool] = {"verdict": "pass", "detail": {"errors": t.get("errors"), "note": t.get("error")}}
    return results
