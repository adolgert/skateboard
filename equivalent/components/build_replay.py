"""Wraps the builder's /v1/build as a gateway component.

Trust role: what this returns becomes a claim. The builder is trusted for
what it measures (it runs agent code); this component only carries the
tree to it and turns its response into a verdict. It never reads the
agent's own working copy -- only the gateway's own committed tree.

The build is the tree's own makefile, so "it compiled" is no longer
enough to say the strategy was honored. Two further statements come back
from the builder's compiler log, and both are verdicts here: the
strategy's flags reached every compile, and every file compiled was the
submitted tree's own source. A build that succeeded while ignoring the
flags is a `fail` with the offending command line named, not a pass.
"""
from __future__ import annotations

from equivalent.manifest.schema import source_files

from .building import Recipe, build_verdict
from .context import CheckContext
from .errors import ComponentError
from .result import CheckResult


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Build the region's current tree with the strategy's own flags.

    The flags come from the strategy file and the build recipe from the
    code's manifest; nothing about either lives in the builder. The
    builder echoes back every compiler command line it saw, and that is
    what goes into the claim's detail.

    A port is built one way, so the provenance answers with one strategy
    and the claim's detail is that build's, flat. What each build means is
    build_verdict's, which the onboarding build asks the same question of.

    Raises ComponentError if the builder call itself couldn't be completed
    (not a verdict about the code).
    """
    manifest = ctx.provenance.manifest()
    if not source_files(manifest, sorted(ctx.tree.files)):
        raise ComponentError(
            f"no file in tree {ctx.tree.sha} at ref {ctx.tree.ref} matches the source "
            f"patterns of code '{manifest.name}'"
        )
    strategy, = ctx.provenance.strategies()
    return build_verdict(
        ctx.builder, ctx.provenance.attempt_id(strategy), ctx.tree.payload(),
        strategy, Recipe.from_manifest(manifest),
    )
