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
from equivalent.ledger.table import ACTION_TABLE


@dataclass(frozen=True)
class Handler:
    """One action's check, and the tool name its claims are filed under."""

    check: object
    tool: str


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
    "time_baseline": Handler(timing.check_baseline, "builder"),

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
    """Every dispatchable row has a check, and every check has a row."""
    dispatchable = {row.name for row in ACTION_TABLE if row.component is not None}
    without_handler = sorted(dispatchable - set(HANDLERS))
    without_row = sorted(set(HANDLERS) - dispatchable)
    if without_handler or without_row:
        raise RuntimeError(
            f"the action table and the handler table disagree: {without_handler} name a "
            f"component and have no check, {without_row} have a check and no row"
        )


_parity()
