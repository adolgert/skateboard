"""Which check answers which action.

Trust role: this is the one place an action name becomes a call. A row of
the precondition table that named no check would be an action the
gateway accepted and then quietly did nothing about; a check nothing
dispatches to is a check nobody runs. Both are mistakes that only show
up in a session's results, so the table below is compared against the
action table when this module is imported, and a gateway with either
kind of hole does not come up.

The tool name beside each check is what the claim records as the thing
that reached the verdict, which is what a person reading a ledger looks
at first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Union

from equivalent.components import (
    build_replay,
    harness_build,
    harness_capture,
    harness_determinism,
    harness_property,
    harness_replay,
    harness_self_check,
    harness_timing,
    manifest_check,
    original_check,
    program_regression,
    property_check,
    regression,
    run_replay,
    sanitize,
    sese_check,
    timing,
)
from equivalent.components.context import CheckContext
from equivalent.components.result import CheckResult
from equivalent.ledger.table import ACTION_TABLE, SUBJECT_KIND_OF
from equivalent.ledger.workflow import ACTIONS

# The context the gateway resolved and the settings the session asked
# for, in; a verdict out.
Check = Callable[[CheckContext, dict], Union[CheckResult, Mapping[str, CheckResult]]]


@dataclass(frozen=True)
class Handler:
    """One action's check, and the tool name its claims are filed under.

    A row that emits one predicate answers with one CheckResult. A row
    that emits several -- sanitize is the only one -- answers with a
    mapping keyed by predicate type, and the gateway files one claim per
    entry of it, all in the one dispatch.
    """

    check: Check
    tool: str
    # Which of the request's subjects this action's claims are filed
    # against, which is the kind its check names in every result it
    # returns. The gateway looks for a repeat of a request under this
    # subject before it dispatches, so a check filing against another
    # would never find its own earlier claim and would run again forever.
    subject_kind: str = "tree"


# Only Python callables belong here. Tool and subject policy come from the
# same catalog the ledger reader and action table use.
CHECKS = {
    'sese_check': sese_check.check,
    'build_replay': build_replay.check,
    'run_replay': run_replay.check,
    'sanitize': sanitize.check,
    'regression_visible': regression.check_visible,
    'property_check': property_check.check,
    'regression_holdout': regression.check_holdout,
    'program_regression': program_regression.check,
    'time_port': timing.check_port,
    'time_baseline': timing.check_baseline,
    'manifest_check': manifest_check.check,
    'harness_build': harness_build.check,
    'harness_capture': harness_capture.check,
    'harness_replay': harness_replay.check,
    'harness_determinism': harness_determinism.check,
    'harness_timing': harness_timing.check,
    'harness_original': original_check.check,
    'harness_self_check': harness_self_check.check,
    'harness_property': harness_property.check,
}


def _handlers() -> dict[str, Handler]:
    names = {action.name for action in ACTIONS}
    if names != set(CHECKS):
        raise RuntimeError(
            f"workflow and checks disagree: missing {sorted(names - set(CHECKS))}, "
            f"unknown {sorted(set(CHECKS) - names)}"
        )
    return {
        action.name: Handler(CHECKS[action.name], action.tool, action.subject_kind)
        for action in ACTIONS
    }


HANDLERS = _handlers()


def _parity() -> None:
    """Every dispatchable row has a check, and every check has a row.

    Two more things are held to the row while we are here. A row whose
    component names a backend must declare that backend in `needs`, or
    the gateway would dispatch to a client it has not got instead of
    answering that it is not configured. And the subject a handler files
    against must be the subject the table says that predicate's claims
    live under, or the duplicate lookup would search where the claim
    never went.
    """
    dispatchable = {row.name for row in ACTION_TABLE if row.dispatchable}
    without_handler = sorted(dispatchable - set(HANDLERS))
    without_row = sorted(set(HANDLERS) - dispatchable)
    if without_handler or without_row:
        raise RuntimeError(
            f"the action table and the handler table disagree: {without_handler} name a "
            f"component and have no check, {without_row} have a check and no row"
        )
    undeclared = sorted(
        f"{row.name} reaches the {backend} but does not declare it"
        for row in ACTION_TABLE if row.dispatchable
        for backend in ("builder", "oracle")
        if row.component.startswith(f"{backend}:") and backend not in row.needs
    )
    misfiled = sorted(
        f"{row.name} files {predicate_type} against {HANDLERS[row.name].subject_kind}, "
        f"which the table records under {SUBJECT_KIND_OF[predicate_type]}"
        for row in ACTION_TABLE if row.dispatchable
        for predicate_type in row.emits
        if predicate_type in SUBJECT_KIND_OF
        and SUBJECT_KIND_OF[predicate_type] != HANDLERS[row.name].subject_kind
    )
    if undeclared or misfiled:
        raise RuntimeError("; ".join((*undeclared, *misfiled)))


_parity()
