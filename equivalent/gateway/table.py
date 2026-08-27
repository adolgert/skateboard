"""The precondition table: one row per action the gateway knows about.

Trust role: this is what a later POST /run (Step 5b) checks before it will
dispatch to anything. A row with the wrong `requires` lets a check run on
evidence it should not trust, or blocks one that is actually ready.

This step (5a) only builds the table's data and exposes it over GET
/table. Reading `requires` to refuse or dispatch a request is Step 5b.
"""
from __future__ import annotations

from dataclasses import dataclass

from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS


@dataclass(frozen=True)
class ActionRow:
    name: str
    emits: tuple  # tuple[str, ...] -- predicate types this action can produce
    requires: tuple  # tuple[tuple[str, str], ...] -- (predicate_type, subject_kind) pairs
    deterministic: bool
    component: str | None  # None only for "accept", which has nothing to dispatch to


ACTION_TABLE = (
    ActionRow("sese_check", ("sese/verified",), (), True, "analyzer:check_sese"),
    ActionRow("build_replay", ("build/replay",), (("sese/verified", "frozen"),), True, "builder:/v1/build"),
    ActionRow("run_replay", ("gpu/executed",), (("build/replay", "tree"),), True, "builder:/v1/run"),
    ActionRow(
        "sanitize", ("sanitize/memcheck", "sanitize/racecheck", "sanitize/initcheck"),
        (("gpu/executed", "tree"),), True, "builder:/v1/sanitize",
    ),
    ActionRow(
        "regression_visible", ("regression/visible",),
        (("sanitize/memcheck", "tree"), ("sanitize/racecheck", "tree")), True, "oracle:/v1/compare",
    ),
    ActionRow(
        "regression_holdout", ("regression/holdout",),
        (("regression/visible", "tree"),), True, "oracle:/v1/compare",
    ),
    ActionRow("time_port", ("timing/port",), (("regression/holdout", "tree"),), False, "builder:/v1/time"),
    ActionRow("time_baseline", ("timing/baseline",), (), False, "builder:/v1/time"),
    ActionRow(
        "accept", (), tuple((r.predicate_type, r.subject_kind) for r in ACCEPTANCE_REQUIREMENTS),
        True, None,
    ),
)
