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

from equivalent.ledger.records import SCHEMA_VERSION
from equivalent.ledger.subjects import Subject, hash_bytes
from equivalent.ledger.vocabulary import EXECUTABLE_IDENTITY_KEY, TARGETS_KEY


BUILD_PREDICATE = {"porting": "build/replay", "onboarding": "harness/builds"}
# The claims that are not judged against an executable cohort: the build
# itself, and the checks that run before any build exists. Written out
# rather than derived from the precondition table, because which claims
# are exempt from the current build is what a reader of this rule most
# needs to see; a test holds it to the table so it cannot drift.
FOUNDATION_PREDICATES = {
    "sese/verified", "manifest/valid", "build/replay", "harness/builds",
    "timing/baseline",
}
NO_CURRENT_BUILD = Subject(
    kind="binary", sha256=hash_bytes(b"equivalent:no-current-build:v1"),
)


def claim_matches_context(claim, required_materials=()) -> bool:
    """Whether a claim was issued under the current evidence contract.

    This is the rule that separates a claim that is evidence about the
    code as it stands from one that only records something that was once
    true. It lives here, beside the cohort the materials come from, and
    the store, status, and the region's allow-list all ask it rather than
    each keeping a copy that could drift.

    Materials are an unordered dependency set. Extra materials describe
    predicate-specific inputs (for example a tolerance policy) and do not
    prevent a match; every caller-supplied current material must be present.
    """
    return (
        claim.version == SCHEMA_VERSION
        and all(material in claim.materials for material in required_materials)
    )


def binary_materials(detail: dict) -> tuple[Subject, ...]:
    """Executable identities retained anywhere in structured claim detail."""
    digests = set()

    def visit(value, key=None):
        if isinstance(value, dict):
            if key == EXECUTABLE_IDENTITY_KEY and isinstance(value.get("sha256"), str):
                digests.add(value["sha256"])
            if key == TARGETS_KEY:
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
