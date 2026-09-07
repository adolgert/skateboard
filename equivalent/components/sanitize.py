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

from equivalent.strategy.schema import Strategy
from equivalent.tree import attempt_id_for

from .context import CheckContext, CheckResult, failed
from .errors import ComponentError

# What each tool's own verdict is filed as. One builder call answers for
# every tool the strategy asked for, and each answer is its own claim.
PREDICATE = "sanitize/{tool}"


def _chosen_cases(strategy: Strategy, visible_cases: dict) -> dict:
    """The cases the strategy asks the sanitizers to run over.

    The strategy loader rejects any value other than "all" or "first", so
    there is no third branch to write here.
    """
    if strategy.sanitize_cases == "all":
        return dict(visible_cases)
    first = next(iter(visible_cases))
    return {first: visible_cases[first]}


def check(ctx: CheckContext, config: dict) -> dict:
    """One verdict per sanitizer the strategy asked for, keyed by predicate type."""
    visible_cases = ctx.visible_cases
    if not visible_cases:
        raise ComponentError("no visible dataset configured for this region")

    attempt_id = attempt_id_for(ctx.region_id, ctx.tree.sha)
    cases = _chosen_cases(ctx.strategy, visible_cases)
    tools = list(ctx.strategy.sanitizers)
    try:
        resp = ctx.builder.sanitize(
            attempt_id, ctx.manifest.build.targets["replay"].executable, cases, tools,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/sanitize call failed: {exc}") from exc

    per_tool = resp.get("per_tool")
    response_problem = None
    if resp.get("stage") != "sanitize":
        response_problem = f"builder returned stage {resp.get('stage')!r}, expected 'sanitize'"
    elif not isinstance(resp.get("ok"), bool):
        response_problem = "builder returned no boolean top-level sanitizer outcome"
    elif not isinstance(per_tool, dict):
        response_problem = "builder returned no per_tool sanitizer results"
        per_tool = {}
    else:
        expected_ok = all(
            isinstance(per_tool.get(tool), dict) and per_tool[tool].get("ok") is True
            for tool in tools
        )
        if resp["ok"] is not expected_ok:
            response_problem = (
                "builder's top-level sanitizer outcome is inconsistent with its "
                "requested per-tool outcomes"
            )

    results = {}
    for tool in tools:
        t = per_tool.get(tool) if isinstance(per_tool, dict) else None
        detail = {
            "errors": t.get("errors") if isinstance(t, dict) else None,
            "log_tail": t.get("log_tail", "") if isinstance(t, dict) else "",
            "cases": sorted(cases),
        }
        if "executable_identity" in resp:
            detail["executable_identity"] = resp["executable_identity"]
        if response_problem:
            reason = response_problem
        elif not isinstance(t, dict):
            reason = f"requested sanitizer '{tool}' is missing from the builder response"
            detail["log_tail"] = resp.get("log_tail", "")
        elif t.get("ok") is not True:
            reason = t.get("error") or f"sanitizer '{tool}' did not complete successfully"
        elif (
            isinstance(t.get("errors"), bool)
            or not isinstance(t.get("errors"), int)
            or t["errors"] != 0
        ):
            reason = f"sanitizer '{tool}' returned an invalid or nonzero error count"
        else:
            reason = None
        if reason is None:
            results[PREDICATE.format(tool=tool)] = CheckResult(verdict="pass", detail=detail)
        else:
            results[PREDICATE.format(tool=tool)] = failed({**detail, "reason": reason}, [reason])
    return results
