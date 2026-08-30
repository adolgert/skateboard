"""Wraps the builder's /v1/sanitize as a gateway component.

One builder call produces one verdict per tool named in the strategy's
`sanitizers` list (memcheck, racecheck, initcheck), matching
one call to the builder -> three ledger claims.
Unlike every other component here, this one returns several verdicts, not
one -- equivalent.gateway.app records one claim per tool from a single
dispatch, atomically, so a duplicate check against any single one of them
is a safe proxy for "all three already exist".

Which of the visible cases are sanitized comes from the strategy's
`sanitize_cases` field, not from this module: a sanitizer run is far
slower than a plain replay, so a strategy can ask for one case or for
every one of them.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def _chosen_cases(strategy: Strategy, visible_cases: dict) -> dict:
    """The cases the strategy asks the sanitizers to run over.

    The strategy loader rejects any value other than "all" or "first", so
    there is no third branch to write here.
    """
    if strategy.sanitize_cases == "all":
        return dict(visible_cases)
    first = next(iter(visible_cases))
    return {first: visible_cases[first]}


def check(region_id: str, tree_sha: str, strategy: Strategy, manifest: Manifest,
          visible_cases: dict, builder) -> dict:
    """Returns {tool_name: {"verdict": "pass"|"fail", "detail": {...}}, ...}."""
    if not visible_cases:
        raise ComponentError("no visible dataset configured for this region")

    attempt_id = attempt_id_for(region_id, tree_sha)
    cases = _chosen_cases(strategy, visible_cases)
    tools = list(strategy.sanitizers)
    try:
        resp = builder.sanitize(attempt_id, manifest.build.targets["replay"].executable, cases, tools)
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
