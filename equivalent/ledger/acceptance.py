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

This lives in the ledger package because the ledger CLI must be able to
check claims against it without the gateway installed. The workflow catalog
owns the action definitions; this module derives requirements from their
acceptance roles and applies manifest conditions.

All three sanitizer modes block acceptance.  In particular, an initcheck
failure cannot be hidden behind passing memcheck and racecheck claims.
Timing/baseline is a one-time claim made on the baseline tree. It becomes
part of a port's required performance/speedup verdict rather than being a
claim about that port itself.
"""
from __future__ import annotations

from dataclasses import dataclass

from .workflow import (
    ACTIONS, AcceptanceRole, FINISHED_WORD, ONBOARDING, PHASES, PORTING,
)


@dataclass(frozen=True)
class Requirement:
    predicate_type: str
    # Which subject the claim that meets this has to be about: the tree
    # that was submitted, or the baseline files the allow-list holds
    # still around it. The precondition table beside this uses a wider
    # vocabulary -- one of its rows rests on a claim about the pristine
    # baseline tree -- but a requirement is only ever about these two.
    subject_kind: str  # "tree" or "frozen"
    producing_action: str


def _requirements(phase: str, role: AcceptanceRole) -> tuple[Requirement, ...]:
    return tuple(
        Requirement(predicate.name, action.subject_kind, action.name)
        for action in ACTIONS if action.phase == phase and action.acceptance is role
        for predicate in action.predicates
    )


ACCEPTANCE_REQUIREMENTS = _requirements(PORTING, AcceptanceRole.REQUIRED)
CONDITIONAL_REQUIREMENTS = _requirements(PORTING, AcceptanceRole.PROPERTIES)
ONBOARDING_REQUIREMENTS = _requirements(ONBOARDING, AcceptanceRole.REQUIRED)
REQUIREMENTS_BY_PHASE = {
    ONBOARDING: ONBOARDING_REQUIREMENTS,
    PORTING: ACCEPTANCE_REQUIREMENTS,
}


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
