"""What a finished region needs before it counts as done.

There are two kinds of session and so two lists. Onboarding brings a code
in far enough to be ported: the manifest it will be checked against, and
the harness built, capturing, replaying, deterministic, and timed.
Porting takes one region of a code that has been through that and ports
it. A region's phase says which list it is judged by.

Trust role: the definition of done. A requirement missing from either
list lets a region be finished without that evidence; the gateway's /run
gate and /status both derive from them.

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


# The two phases a region can be in. A region config names one, and it is
# what picks the requirement list, the action rows, and the word `status`
# prints when everything on the list has passed.
ONBOARDING = "onboarding"
PORTING = "porting"
PHASES = (ONBOARDING, PORTING)

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

# Everything an onboarding session has to leave behind before a person
# reviews what passed and promotes it. All of it is about the tree the
# agent submitted: onboarding rewrites the build, the drivers, and the
# manifest, so there is no part of the tree an earlier claim still covers.
ONBOARDING_REQUIREMENTS = (
    Requirement("manifest/valid", "tree", "manifest_check"),
    Requirement("harness/builds", "tree", "harness_build"),
    Requirement("harness/captured", "tree", "harness_capture"),
    Requirement("harness/replays", "tree", "harness_replay"),
    Requirement("harness/deterministic", "tree", "harness_determinism"),
    Requirement("harness/times", "tree", "harness_timing"),
)

REQUIREMENTS_BY_PHASE = {
    ONBOARDING: ONBOARDING_REQUIREMENTS,
    PORTING: ACCEPTANCE_REQUIREMENTS,
}

# What `status` calls a region that has met every requirement of its
# phase. The words differ because the two mean different things: an
# onboarded code is ready to be reviewed and promoted, an accepted port
# is ready to be merged.
FINISHED_WORD = {ONBOARDING: "ONBOARDED", PORTING: "ACCEPTED"}


def requirements_for(phase: str) -> tuple:
    """The requirement list a region in this phase is judged by."""
    if phase not in REQUIREMENTS_BY_PHASE:
        raise ValueError(f"unknown phase {phase!r}; it must be one of {list(PHASES)}")
    return REQUIREMENTS_BY_PHASE[phase]
