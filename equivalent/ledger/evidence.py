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
from equivalent.ledger.artifacts import build_binary_materials


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
NO_CURRENT_TIMING_CLAIMS = Subject(
    kind="timing_claim", sha256=hash_bytes(b"equivalent:no-current-timing-claims:v1"),
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


def current_build_claim(store, phase: str, tree: Subject, core_materials=()):
    """The current passing build assertion for one candidate tree, if any."""
    claim = store.latest(
        BUILD_PREDICATE[phase], tree, required_materials=core_materials,
    )
    return claim if claim is not None and claim.predicate.verdict == "pass" else None


def dependent_materials(core_materials=(), cohort=()) -> tuple[Subject, ...]:
    """The context a claim that rests on a build has to have been reached under.

    The caller's own context plus every executable the current build
    named. When there is no such build the sentinel stands in and nothing
    matches it: a claim about executables nobody holds any more is not
    evidence about the code as it stands. Said once here because the
    status computation and the gateway's /run gate both ask it, and two
    answers would let a request dispatch on evidence status calls stale.
    """
    return (*core_materials, *(cohort or (NO_CURRENT_BUILD,)))


def timing_claim_material(claim) -> Subject:
    """A formal reference to the exact timing observation a verdict compares.

    Timing is an observation rather than a stable build property.  A later
    baseline or port measurement must therefore retire a speedup verdict made
    against the earlier observation, even when both claims are on unchanged
    trees and use the same binaries.
    """
    return Subject(
        kind="timing_claim",
        sha256=hash_bytes(
            f"equivalent:timing-claim:v1:{claim.predicateType}:{claim.id}".encode("utf-8")
        ),
    )


def current_timing_claim(store, predicate_type: str, subject: Subject | None, core_materials=()):
    """The latest current passing timing claim on its known tree, if any.

    A timing/baseline claim is about the pristine baseline tree, not merely
    any baseline observation in a region ledger.  Callers that cannot name
    that tree have insufficient source context to select a comparison and
    must leave performance acceptance unmet.
    """
    if subject is None:
        return None
    claim = store.latest(predicate_type, subject, required_materials=core_materials)
    return claim if claim is not None and claim.predicate.verdict == "pass" else None


def performance_materials(
    store, tree: Subject, baseline_tree: Subject | None, core_materials=(),
    *, port_materials=None,
):
    """The current port and baseline observations a speedup verdict rests on.

    Port timing rests on its current build cohort; baseline timing predates
    that build and rests only on the core deployment context.  Selecting the
    port claim under the wrong context can make a newer timing observation
    from a superseded binary permanently prevent a performance verdict.
    """
    port = current_timing_claim(
        store, "timing/port", tree,
        core_materials if port_materials is None else port_materials,
    )
    baseline = current_timing_claim(
        store, "timing/baseline", baseline_tree, core_materials,
    )
    if port is None or baseline is None:
        return (NO_CURRENT_TIMING_CLAIMS,)
    return (timing_claim_material(port), timing_claim_material(baseline))


def required_materials_by_predicate(
    store, requirements, phase: str, tree: Subject, core_materials=(), baseline_tree=None,
) -> dict[str, tuple[Subject, ...]]:
    """Material context for each dependent predicate in status/promotion.

    Foundation claims predate the executable cohort and continue to use the
    caller's core context; every later claim is judged by the rule above.
    """
    build_claim = current_build_claim(store, phase, tree, core_materials)
    cohort = (
        build_binary_materials(BUILD_PREDICATE[phase], build_claim.predicate.detail)
        if build_claim else ()
    )
    dependent = dependent_materials(core_materials, cohort)
    required = {
        requirement.predicate_type: dependent
        for requirement in requirements
        if requirement.predicate_type not in FOUNDATION_PREDICATES
    }
    if "performance/speedup" in required:
        required["performance/speedup"] = (
            *dependent,
            *performance_materials(
                store, tree, baseline_tree, core_materials,
                port_materials=dependent,
            ),
        )
    return required
