"""The precondition table: one row per action the gateway knows about.

Trust role: this is what POST /run checks before it will dispatch to
anything. A row with the wrong `requires` lets a check run on evidence
it should not trust, or blocks one that is actually ready.

Every row belongs to one phase. A region is in one phase for its whole
life, so the rows a session ever sees are the rows of its region's
phase; the table holds both because one gateway serves regions of both
kinds, and because a reader of an old ledger has to be able to look up
an action of either.
"""
from __future__ import annotations

from dataclasses import dataclass

from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    ONBOARDING,
    ONBOARDING_REQUIREMENTS,
    PORTING,
    acceptance_requirements,
)

# The row that names a phase's whole requirement list rather than an
# action to dispatch. The porting one is the only row whose preconditions
# depend on the code, so it is spelled here for `requires_for` to find.
ACCEPT = "accept"


@dataclass(frozen=True)
class ActionRow:
    name: str
    emits: tuple  # tuple[str, ...] -- predicate types this action can produce
    requires: tuple  # tuple[tuple[str, str], ...] -- (predicate_type, subject_kind) pairs
    deterministic: bool
    # None only for the row that names a phase's whole requirement list --
    # "accept" and "onboarded" -- which has nothing to dispatch to.
    component: str | None
    # Which kind of session this action belongs to. It is written on every
    # row rather than inferred from the name so that GET /table can serve
    # one phase's rows without a second list saying which those are.
    phase: str
    # The config keys POST /run accepts for this action; anything else is
    # rejected before dispatch. This keeps the config canonical, so
    # duplicate detection (which hashes the config) can't be defeated by
    # padding a request with junk keys until it hashes differently.
    config_keys: tuple = ()


ACTION_TABLE = (
    # Porting: one region of a code that has already been brought in.
    ActionRow("sese_check", ("sese/verified",), (), True, "analyzer:check_sese", PORTING),
    ActionRow("build_replay", ("build/replay",), (("sese/verified", "frozen"),), True,
              "builder:/v1/build", PORTING),
    ActionRow("run_replay", ("gpu/executed",), (("build/replay", "tree"),), True,
              "builder:/v1/run", PORTING),
    ActionRow(
        "sanitize", ("sanitize/memcheck", "sanitize/racecheck", "sanitize/initcheck"),
        (("gpu/executed", "tree"),), True, "builder:/v1/sanitize", PORTING,
    ),
    ActionRow(
        "regression_visible", ("regression/visible",),
        (("sanitize/memcheck", "tree"), ("sanitize/racecheck", "tree")), True,
        "oracle:/v1/compare", PORTING,
    ),
    ActionRow(
        "property_check", ("regression/property",),
        (("regression/visible", "tree"),), True, "builder:/v1/properties", PORTING,
        config_keys=("seed", "max_examples"),
    ),
    ActionRow(
        "regression_holdout", ("regression/holdout",),
        (("regression/visible", "tree"),), True, "oracle:/v1/compare", PORTING,
    ),
    ActionRow(
        "program_regression", ("program/regression",),
        (("regression/holdout", "tree"),), True, "builder:/v1/time", PORTING,
    ),
    ActionRow("time_port", ("timing/port",), (("program/regression", "tree"),), False,
              "builder:/v1/time", PORTING, config_keys=("repeats",)),
    ActionRow("time_baseline", ("timing/baseline",), (), False, "builder:/v1/time", PORTING,
              config_keys=("repeats",)),
    ActionRow(
        "accept", (), tuple((r.predicate_type, r.subject_kind) for r in ACCEPTANCE_REQUIREMENTS),
        True, None, PORTING,
    ),

    # Onboarding: bringing a code in, in the order one step's evidence
    # becomes the next step's precondition.
    ActionRow("manifest_check", ("manifest/valid",), (), True, "gateway:manifest_check", ONBOARDING),
    ActionRow("harness_build", ("harness/builds",), (("manifest/valid", "tree"),), True,
              "builder:/v1/build", ONBOARDING),
    ActionRow("harness_capture", ("harness/captured",), (("harness/builds", "tree"),), True,
              "builder:/v1/capture", ONBOARDING),
    ActionRow("harness_replay", ("harness/replays",), (("harness/captured", "tree"),), True,
              "builder:/v1/run", ONBOARDING),
    ActionRow("harness_determinism", ("harness/deterministic",), (("harness/replays", "tree"),),
              True, "builder:/v1/capture", ONBOARDING),
    ActionRow("harness_timing", ("harness/times",), (("harness/builds", "tree"),), True,
              "builder:/v1/time", ONBOARDING),
    ActionRow(
        "onboarded", (), tuple((r.predicate_type, r.subject_kind) for r in ONBOARDING_REQUIREMENTS),
        True, None, ONBOARDING,
    ),
)


def rows_for(phase: str) -> tuple:
    """The rows a region in this phase may run, in the order they are listed."""
    return tuple(row for row in ACTION_TABLE if row.phase == phase)


def requires_for(row: ActionRow, manifest=None) -> tuple:
    """One row's preconditions for a particular code.

    Every row but one has the preconditions written on it. The `accept`
    row is the exception: what finishes a port depends on whether the code
    declares a module of invariants, so its list is read from the code's
    manifest rather than frozen into the table.
    """
    if row.name != ACCEPT:
        return row.requires
    return tuple(
        (req.predicate_type, req.subject_kind) for req in acceptance_requirements(manifest)
    )
