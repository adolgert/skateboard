"""The precondition table: one row per action the gateway knows about.

Trust role: this is what POST /run checks before it will dispatch to
anything. A row with the wrong `requires` lets a check run on evidence
it should not trust, or blocks one that is actually ready.
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
    # The config keys POST /run accepts for this action; anything else is
    # rejected before dispatch. This keeps the config canonical, so
    # duplicate detection (which hashes the config) can't be defeated by
    # padding a request with junk keys until it hashes differently. Every
    # row is empty today -- no component reads its config yet. When a row
    # gains keys, also expose them through GET /table so the extension can
    # generate tool parameters from them.
    config_keys: tuple = ()


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
    ActionRow("time_port", ("timing/port",), (("regression/holdout", "tree"),), False, "builder:/v1/time",
              config_keys=("repeats",)),
    ActionRow("time_baseline", ("timing/baseline",), (), False, "builder:/v1/time",
              config_keys=("repeats",)),
    ActionRow(
        "accept", (), tuple((r.predicate_type, r.subject_kind) for r in ACCEPTANCE_REQUIREMENTS),
        True, None,
    ),
)
