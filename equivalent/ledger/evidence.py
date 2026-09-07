"""Which claims a later claim has to have been reached on top of.

Trust role: this decides what counts as current. A check that passed
against an executable nobody builds any more is not evidence about the
code as it stands, so every claim that depends on a build carries the
identities of the build that is current, and a claim carrying anything
else is stale. Getting this wrong lets an old pass stand in for a new
one.

The build itself, and the few checks that precede any build, are the
exception: they are the foundation the cohort is derived from, so they
are judged on the caller's own context.
"""
from __future__ import annotations

from equivalent.ledger.subjects import Subject, hash_bytes


BUILD_PREDICATE = {"porting": "build/replay", "onboarding": "harness/builds"}
FOUNDATION_PREDICATES = {
    "sese/verified", "manifest/valid", "build/replay", "harness/builds",
    "timing/baseline",
}
NO_CURRENT_BUILD = Subject(
    kind="binary", sha256=hash_bytes(b"equivalent:no-current-build:v1"),
)


def binary_materials(detail: dict) -> tuple[Subject, ...]:
    """Executable identities retained anywhere in structured claim detail."""
    digests = set()

    def visit(value, key=None):
        if isinstance(value, dict):
            if key == "executable_identity" and isinstance(value.get("sha256"), str):
                digests.add(value["sha256"])
            if key == "targets":
                for target in value.values() if isinstance(value, dict) else ():
                    if isinstance(target, dict) and isinstance(target.get("sha256"), str):
                        digests.add(target["sha256"])
            for child_key, child in value.items():
                visit(child, child_key)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(detail)
    return tuple(Subject(kind="binary", sha256=digest) for digest in sorted(digests))


def current_build_claim(store, phase: str, tree: Subject, core_materials=()):
    """The current passing build assertion for one candidate tree, if any."""
    claim = store.latest(
        BUILD_PREDICATE[phase], tree, required_materials=core_materials,
    )
    return claim if claim is not None and claim.predicate.verdict == "pass" else None


def required_materials_by_predicate(
    store, requirements, phase: str, tree: Subject, core_materials=(),
) -> dict[str, tuple[Subject, ...]]:
    """Material context for each dependent predicate in status/promotion.

    Foundation claims predate the executable cohort and continue to use the
    caller's core context. Every later claim must contain all identities from
    the current passing build. The sentinel makes dependent legacy evidence
    stale when no usable build cohort exists.
    """
    build_claim = current_build_claim(store, phase, tree, core_materials)
    cohort = binary_materials(build_claim.predicate.detail) if build_claim else ()
    dependent = (*core_materials, *(cohort or (NO_CURRENT_BUILD,)))
    return {
        requirement.predicate_type: dependent
        for requirement in requirements
        if requirement.predicate_type not in FOUNDATION_PREDICATES
    }
