"""What varies between bringing a code in and porting one.

Trust role: onboarding and porting run the same builder against the same
trees, and five things vary together between them -- which manifest
describes the code, which strategies a build is asked under, what the
builder's workspace for one is called, which predicate its claim is filed
as, and what shape that claim's detail takes. Spelled at each call site
they drift: a check that read the wrong manifest, or a gateway that
looked for a build under a workspace name nothing built under, would be
judging something other than what was submitted. Written once per phase
here, they cannot.

This is a module of its own so that the record a check is handed and the
record it gives back stay a description of one request, with the policy
that answers for a phase beside them rather than inside them. A context
asks for its own provenance; nothing here asks a context for anything but
what it holds.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from equivalent.ledger.acceptance import ONBOARDING, PORTING
from equivalent.ledger.evidence import BUILD_PREDICATE
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy

from .errors import ComponentError, after_the_manifest_check_passed
from .result import CheckResult, failed
from .workspaces import attempt_id_for, attempt_id_for_strategy

if TYPE_CHECKING:
    from .context import CheckContext


class Provenance:
    """Where a build's description comes from, in one phase.

    `build_predicate` and `oracle_judges` are answered by the class, so the
    gateway can ask them with only a region's phase in hand; the rest are
    about the tree in one context and are answered by an instance. Stored
    build detail is decoded by ledger.artifacts, beside the evidence rule
    that consumes it.
    """

    build_predicate: str
    # Whether the oracle has anything to say about a region in this phase.
    # Only a port is compared against held-out answers, so a gateway with
    # no oracle can still bring a code in.
    oracle_judges: bool

    def __init__(self, ctx: "CheckContext"):
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

    def _no_target(self, message: str, described: dict):
        raise ComponentError(message)


PROVENANCE = {ONBOARDING: OnboardingProvenance, PORTING: PortingProvenance}


def provenance_for(phase: str):
    """The one class that answers for a phase. The only place phase decides."""
    try:
        return PROVENANCE[phase]
    except KeyError:
        raise ComponentError(f"no provenance for phase '{phase}'") from None
