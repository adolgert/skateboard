"""What a finished port needs before a region counts as accepted.

Trust role: the definition of done. A requirement missing from this list
lets a port be ACCEPTED without that evidence; the gateway's /run gate
and /status both derive from it.

This lives in the ledger package, not next to the gateway's precondition
table, because the ledger CLI must be able to check claims against it
without the gateway installed. The table's "accept" row imports this
list rather than writing a second copy.

Matches the first demonstration harness's actual gate: sanitize/initcheck
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
