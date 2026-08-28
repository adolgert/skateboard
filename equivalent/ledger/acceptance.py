"""What a finished region needs before it counts as done.

There are two kinds of session and so two lists. Onboarding brings a code
in far enough to be ported: the manifest it will be checked against, and
the harness built, capturing, replaying, deterministic, and timed.
Porting takes one region of a code that has been through that and ports
it. A region's phase says which list it is judged by.

Most of what is on the two lists is the same for every code. One entry is
not: a code may carry its own module of invariants, and a port of it has
to pass those as well. That requirement is therefore read with the code's
manifest in hand -- present for a code that declares a properties module,
absent for one that does not, because a code with no invariants written
down has nothing to fail.

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
    Requirement("program/regression", "tree", "program_regression"),
    Requirement("timing/port", "tree", "time_port"),
)

# What a port has to pass as well when its code declares invariants of its
# own. It is separate from the list above rather than in it because
# whether it applies is a fact about the code, not about porting.
CONDITIONAL_REQUIREMENTS = (
    Requirement("regression/property", "tree", "property_check"),
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
    Requirement("harness/self_check", "tree", "harness_self_check"),
    Requirement("harness/properties", "tree", "harness_property"),
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


def acceptance_requirements(manifest=None) -> tuple:
    """What a port is judged by, for the code the manifest describes.

    A code that names a properties module has to pass it; one that does
    not has nothing there to pass. `manifest` is None for a reader with no
    code to ask -- the ledger CLI pointed at a bare directory -- and then
    the fixed list is the answer, because reporting a requirement nobody
    can say applies would call a finished region unfinished.
    """
    if manifest is not None and manifest.properties is not None:
        return (*ACCEPTANCE_REQUIREMENTS, *CONDITIONAL_REQUIREMENTS)
    return ACCEPTANCE_REQUIREMENTS


def requirements_for(phase: str, manifest=None) -> tuple:
    """The requirement list a region in this phase is judged by.

    Onboarding's list is the same for every code: what it produces is the
    description a properties module would be named in, so there is nothing
    to read out of a manifest yet.
    """
    if phase not in REQUIREMENTS_BY_PHASE:
        raise ValueError(f"unknown phase {phase!r}; it must be one of {list(PHASES)}")
    if phase == PORTING:
        return acceptance_requirements(manifest)
    return REQUIREMENTS_BY_PHASE[phase]
