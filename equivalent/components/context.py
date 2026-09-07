"""What every check is given, and what every check gives back.

Trust role: this is the boundary that keeps a check honest. A check is
handed the tree it judges, the strategies it judges under, the claims it
is allowed to rest on, and a reader for the arrays those claims name --
and nothing that could file evidence. It answers with a verdict, the
detail that becomes the claim, and a declaration of what the verdict
rests on and what should be kept. Everything that writes to the ledger
is on the gateway's side of this line, so a check that fails cannot
leave a set behind for the next check to find, and a check cannot file a
claim about a question nobody asked.

The claims are looked up by the gateway before the check runs, against
the region's precondition table. So a check reads `ctx.claims[...]`
without asking whether the claim is there: if it were not, the gateway
would have refused the request and named the action that produces it.

The context also carries the provenance below: where this region's phase
gets the manifest, the strategies, and the workspace names a build is
described by. A check asks it rather than deciding for itself which
phase it is in, and so does the gateway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Mapping

from equivalent.ledger.acceptance import ONBOARDING, PORTING
from equivalent.ledger.evidence import BUILD_PREDICATE
from equivalent.ledger.records import Claim
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy
from equivalent.tree import Tree, attempt_id_for, attempt_id_for_strategy

from .datasets import load_visible_cases
from .errors import ComponentError, after_the_manifest_check_passed


@dataclass(frozen=True)
class CheckContext:
    """One region, one tree, at the moment a check is asked to judge it."""

    region_id: str
    phase: str
    # The candidate tree, resolved to a commit before anything ran, and
    # the pristine baseline it is a port of. `tree.sha` is the subject
    # every claim about this request is filed against.
    tree: Tree
    baseline: Tree
    strategy: Strategy
    baseline_strategy: Strategy
    # The promoted manifest of the code this region belongs to. A region
    # being onboarded has none -- the manifest it is judged by is the one
    # inside the tree, which those checks read from `tree` themselves.
    manifest: Manifest | None = None
    spec_path: str | None = None
    original_reference_path: Path | None = None
    visible_dataset: Path | None = None
    # The backends, or None when this gateway has none configured. The
    # table says which actions need which, so a check that is dispatched
    # has the ones it names.
    builder: object | None = None
    oracle: object | None = None
    # The current passing claim for each predicate type this action rests
    # on, keyed by predicate type.
    claims: Mapping[str, Claim] = field(default_factory=dict)
    # Read-only access to the capture sets the region's ledger holds
    # (equivalent.ledger.capture_sets.SetReader), or None where a check
    # reads none.
    sets: object | None = None

    @cached_property
    def visible_cases(self) -> dict:
        """The region's visible inputs, read from disk for this request.

        Read now rather than kept from an earlier one: the digest of
        these bytes is in the request's evidence context, and an old
        in-memory copy would make that material describe different
        inputs.
        """
        if self.visible_dataset is None:
            raise ComponentError(f"no visible dataset configured for region {self.region_id}")
        return load_visible_cases(self.visible_dataset, self.manifest)

    @cached_property
    def provenance(self) -> "Provenance":
        """Where this request's checks get what a build is described by."""
        return provenance_for(self.phase)(self)


@dataclass(frozen=True)
class CheckResult:
    """A verdict, and everything the gateway needs to file it.

    `detail` is what goes into the claim, unchanged: what a ledger holds
    on disk is not this module's to rename. `reasons` are the same
    failure said in words for the session that asked, beside the claim in
    the answer. `materials` are the formal things this verdict rests on
    -- a tolerance policy, a capture set, a reference -- declared by the
    check that knows them rather than fished out of the detail by the
    gateway. `stores` are what the gateway should keep before it records:
    a set that was packed and is not listed here is thrown away.
    """

    verdict: str
    detail: dict
    reasons: tuple = ()
    materials: tuple = ()
    stores: tuple = ()
    # Which of the request's subjects the claim is filed against. Almost
    # every claim is about the candidate tree; a baseline timing is about
    # the baseline.
    subject_kind: str = "tree"


def failed(detail: dict, reasons) -> CheckResult:
    """A fail whose detail already holds the same words the session reads."""
    return CheckResult(verdict="fail", detail=detail, reasons=tuple(reasons))


def capture_set_materials(detail: dict) -> tuple:
    """The capture sets a claim's own detail names, as materials.

    The harness checks that read or write a dataset all name it the same
    way in their detail, so they declare their materials the same way
    here. A verdict reached against one set of captured arrays must not
    read as a verdict against another, which is what putting them in
    materials says.
    """
    return tuple(
        Subject(kind="capture_set", sha256=entry["capture_set"])
        for _, entry in sorted(detail.get("datasets", {}).items())
        if entry.get("capture_set")
    )


class Provenance:
    """Where a build's description comes from, in one phase.

    Trust role: onboarding and porting run the same builder against the
    same trees, and five things vary together between them -- which
    manifest describes the code, which strategies a build is asked under,
    what the builder's workspace for one is called, which predicate its
    claim is filed as, and what shape that claim's detail takes. Spelled
    at each call site they drift: a check that read the wrong manifest, or
    a gateway that looked for a build under a workspace name nothing built
    under, would be judging something other than what was submitted.
    Written once per phase here, they cannot.

    `build_predicate`, `build_entries` and `oracle_judges` are answered by
    the class, so the gateway can ask them with only a region's phase in
    hand; the rest are about the tree in one context and are answered by
    an instance.
    """

    build_predicate: str
    # Whether the oracle has anything to say about a region in this phase.
    # Only a port is compared against held-out answers, so a gateway with
    # no oracle can still bring a code in.
    oracle_judges: bool

    def __init__(self, ctx: CheckContext):
        self.ctx = ctx

    def manifest(self) -> Manifest:
        """The manifest that describes the code this tree is."""
        raise NotImplementedError

    def strategies(self) -> tuple:
        """The strategies a build of this tree is asked under."""
        raise NotImplementedError

    def attempt_id(self, strategy: Strategy | None = None) -> str:
        """The builder workspace this tree's actions share.

        Named for a strategy where a build under two of them would
        otherwise share one workspace and read each other's object files.
        """
        raise NotImplementedError

    @staticmethod
    def build_entries(detail: dict) -> list:
        """The (workspace, targets) pairs one build claim's detail asserts."""
        raise NotImplementedError

    def build_target(self, manifest: Manifest, role: str, purpose: str, described=None):
        """The target the manifest names for `role`, or what to say instead.

        Answers `(target, None)` when the manifest names one. Whether a
        manifest that names none is the code's fault or the harness's
        depends on whose manifest it is. While a code is being brought in
        the manifest is the agent's own work, so a missing target is a
        `fail` verdict about it and comes back as `(None, verdict)` for
        the check to return. A porting region is judged by a manifest that
        was promoted after review, so a missing target there is a fault on
        the harness's side and this raises instead of answering.
        """
        target = manifest.build.targets.get(role)
        if target is not None:
            return target, None
        return None, self._no_target(
            f"code '{manifest.name}' declares no '{role}' build target, so there is "
            f"{purpose}",
            described or {},
        )

    def _no_target(self, message: str, described: dict):
        raise NotImplementedError


class OnboardingProvenance(Provenance):
    """A code being brought in: its manifest and its harness are the tree's own."""

    build_predicate = BUILD_PREDICATE[ONBOARDING]
    oracle_judges = False

    def manifest(self) -> Manifest:
        with after_the_manifest_check_passed():
            return self.ctx.tree.manifest()

    def strategies(self) -> tuple:
        """Both of them.

        A makefile that honors one compiler's flags and quietly hard-codes
        another's is exactly what bringing a code in is meant to catch.
        """
        return (self.ctx.baseline_strategy, self.ctx.strategy)

    def attempt_id(self, strategy: Strategy | None = None) -> str:
        """The workspace of one strategy's build, the baseline's by default.

        Every onboarding check after the build runs against the build the
        captured answers came from, which is the baseline strategy's.
        """
        one = self.ctx.baseline_strategy if strategy is None else strategy
        return attempt_id_for_strategy(self.ctx.region_id, self.ctx.tree.sha, one.name)

    @staticmethod
    def build_entries(detail: dict) -> list:
        return [
            (one.get("attempt_id"), one.get("targets", {}))
            for _, one in sorted(detail.get("strategies", {}).items())
            if isinstance(one, dict)
        ]

    def _no_target(self, message: str, described: dict) -> CheckResult:
        return failed({**described, "problems": [message]}, [message])


class PortingProvenance(Provenance):
    """A port of a code already brought in: its manifest was promoted after review."""

    build_predicate = BUILD_PREDICATE[PORTING]
    oracle_judges = True

    def manifest(self) -> Manifest:
        return self.ctx.manifest

    def strategies(self) -> tuple:
        return (self.ctx.strategy,)

    def attempt_id(self, strategy: Strategy | None = None) -> str:
        """One workspace per (region, tree): a port is built one way."""
        return attempt_id_for(self.ctx.region_id, self.ctx.tree.sha)

    @staticmethod
    def build_entries(detail: dict) -> list:
        return [(detail.get("attempt_id"), detail.get("targets", {}))]

    def _no_target(self, message: str, described: dict):
        raise ComponentError(message)


PROVENANCE = {ONBOARDING: OnboardingProvenance, PORTING: PortingProvenance}


def provenance_for(phase: str):
    """The one class that answers for a phase. The only place phase decides."""
    try:
        return PROVENANCE[phase]
    except KeyError:
        raise ComponentError(f"no provenance for phase '{phase}'") from None
