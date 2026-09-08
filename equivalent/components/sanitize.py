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

from equivalent.ledger.artifacts import binary_artifacts
from equivalent.ledger.vocabulary import EXECUTABLE_IDENTITY_KEY, PASS
from equivalent.strategy.schema import Strategy

from . import backend
from .context import CheckContext
from .result import CheckResult, failed
from .errors import ComponentError
from .names import REPLAY_ROLE

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

    attempt_id = ctx.provenance.attempt_id()
    cases = _chosen_cases(ctx.strategy, visible_cases)
    tools = list(ctx.strategy.sanitizers)
    replay = ctx.provenance.manifest().build.targets[REPLAY_ROLE]
    resp = backend.sanitize(ctx.builder, attempt_id, replay.executable, cases, tools)

    # One statement about the whole run: the builder said yes exactly when
    # every tool the strategy asked for said yes. A disagreement makes
    # every one of this run's verdicts a failure, because the answer as a
    # whole cannot be read.
    # Every entry of `per_tool` is an object or the answer would not have
    # parsed, so a tool the builder said nothing about is the only way
    # `get` comes back with nothing here.
    expected_ok = all(
        (resp.per_tool.get(tool) or {}).get("ok") is True for tool in tools
    )
    response_problem = None
    if resp.ok is not expected_ok:
        response_problem = (
            "builder's top-level sanitizer outcome is inconsistent with its "
            "requested per-tool outcomes"
        )

    results = {}
    artifacts = binary_artifacts(resp.executable_identity, executable=replay.executable)
    for tool in tools:
        t = resp.per_tool.get(tool)
        detail = {
            "errors": t.get("errors") if t else None,
            "log_tail": t.get("log_tail", "") if t else "",
            "cases": sorted(cases),
        }
        if resp.executable_identity is not None:
            detail[EXECUTABLE_IDENTITY_KEY] = resp.executable_identity
        if response_problem:
            reason = response_problem
        elif t is None:
            reason = f"requested sanitizer '{tool}' is missing from the builder response"
            detail["log_tail"] = resp.log_tail
        elif t.get("ok") is not True:
            reason = t.get("error") or f"sanitizer '{tool}' did not complete successfully"
        elif t.get("errors") != 0:
            reason = f"sanitizer '{tool}' did not report a zero error count"
        else:
            reason = None
        if reason is None:
            results[PREDICATE.format(tool=tool)] = CheckResult(
                verdict=PASS, detail=detail, binary_artifacts=artifacts,
            )
        else:
            results[PREDICATE.format(tool=tool)] = failed(
                {**detail, "reason": reason}, [reason], binary_artifacts=artifacts,
            )
    return results
