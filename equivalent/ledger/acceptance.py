"""What a finished port needs before a region counts as accepted.

This is the "accept" row of the precondition table the plan builds in
Step 5. It's defined here, ahead of Step 5, because the ledger CLI
(Step 3) needs something to check claims against before that table exists.
Step 5 should import this list as the accept row's requirements rather than
writing a second copy.

Matches demo/orchestrator/orchestrator.py's actual gate: sanitize/initcheck
is recorded but does not block acceptance there (only memcheck/racecheck
do), and timing/baseline is a one-time claim made on the baseline tree, not
a requirement of any individual port.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Requirement:
    predicate_type: str
    subject_kind: str  # "tree" or "frozen"
    producing_action: str


ACCEPTANCE_REQUIREMENTS = (
    Requirement("sese/verified", "frozen", "sese_check"),
    Requirement("build/replay", "tree", "build_replay"),
    Requirement("gpu/executed", "tree", "run_replay"),
    Requirement("sanitize/memcheck", "tree", "sanitize"),
    Requirement("sanitize/racecheck", "tree", "sanitize"),
    Requirement("regression/visible", "tree", "regression_visible"),
    Requirement("regression/holdout", "tree", "regression_holdout"),
    Requirement("timing/port", "tree", "time_port"),
)
