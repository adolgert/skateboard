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
from equivalent.components.context import CheckContext, CheckResult
from equivalent.ledger.table import ACTION_TABLE, SUBJECT_KIND_OF

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


HANDLERS = {
    # Porting.
    "sese_check": Handler(sese_check.check, "sese_check"),
    "build_replay": Handler(build_replay.check, "builder"),
    "run_replay": Handler(run_replay.check, "builder"),
    "sanitize": Handler(sanitize.check, "compute-sanitizer"),
    "regression_visible": Handler(regression.check_visible, "oracle"),
    "property_check": Handler(property_check.check, "builder"),
    "regression_holdout": Handler(regression.check_holdout, "oracle"),
    "program_regression": Handler(program_regression.check, "builder"),
    "time_port": Handler(timing.check_port, "builder"),
    "time_baseline": Handler(timing.check_baseline, "builder", subject_kind="baseline_tree"),

    # Onboarding.
    "manifest_check": Handler(manifest_check.check, "manifest_check"),
    "harness_build": Handler(harness_build.check, "builder"),
    "harness_capture": Handler(harness_capture.check, "builder"),
    "harness_replay": Handler(harness_replay.check, "builder"),
    "harness_determinism": Handler(harness_determinism.check, "builder"),
    "harness_timing": Handler(harness_timing.check, "builder"),
    "harness_original": Handler(original_check.check, "builder"),
    "harness_self_check": Handler(harness_self_check.check, "builder"),
    "harness_property": Handler(harness_property.check, "builder"),
}


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
