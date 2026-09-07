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

The context also carries the provenance: where this region's phase gets
the manifest, the strategies, and the workspace names a build is
described by. A check asks it rather than deciding for itself which
phase it is in, and so does the gateway. What each phase answers is
phase.py's, which is written in terms of this record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Mapping

from equivalent.ledger.records import Claim
from equivalent.ledger.subjects import Subject
from equivalent.ledger.vocabulary import CAPTURE_SET_KEY, FAIL
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy
from equivalent.tree import Tree

from .answers import Builder, Oracle
from .datasets import load_visible_cases
from .errors import ComponentError


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
    builder: Builder | None = None
    oracle: Oracle | None = None
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
    def provenance(self):
        """Where this request's checks get what a build is described by."""
        # Asked for here rather than imported above: what a phase answers
        # is written in terms of this record, so that module reads this
        # one and not the other way about.
        from .phase import provenance_for

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
    # Whether this verdict measured an executable that is not the region's
    # own build. A check that builds and runs a program of its own -- the
    # reviewed original the onboarded harness is compared against -- names
    # binaries the current build claim never named, and the gateway would
    # otherwise read those as a build that has moved out from under the
    # claim. The check that knows it built something else says so here,
    # rather than the gateway keeping a list of which checks those are.
    measures_other_binaries: bool = False


def failed(detail: dict, reasons) -> CheckResult:
    """A fail whose detail already holds the same words the session reads."""
    return CheckResult(verdict=FAIL, detail=detail, reasons=tuple(reasons))


def capture_set_materials(detail: dict) -> tuple:
    """The capture sets a claim's own detail names, as materials.

    The harness checks that read or write a dataset all name it the same
    way in their detail, so they declare their materials the same way
    here. A verdict reached against one set of captured arrays must not
    read as a verdict against another, which is what putting them in
    materials says.
    """
    return tuple(
        Subject(kind="capture_set", sha256=entry[CAPTURE_SET_KEY])
        for _, entry in sorted(detail.get("datasets", {}).items())
        if entry.get(CAPTURE_SET_KEY)
    )
