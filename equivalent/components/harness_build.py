"""Builds a submitted tree under both of the region's strategies.

Trust role: what this returns becomes a claim. It is the onboarding
counterpart of build_replay: the same three statements about one build
(it succeeded, the strategy's flags reached every compile, only the
tree's own source was compiled), asked twice -- once for the strategy the
baseline is built with, once for the strategy a port will be built with.

Both are asked here rather than later because a makefile that honors one
compiler's flags and quietly hard-codes another's is exactly what
onboarding is meant to catch, and catching it once, early, is cheaper
than discovering it when a port is already written.

The verdict for each build comes from build_replay, not from a second
copy of the same reasoning here.
"""
from __future__ import annotations

from . import build_replay
from .context import CheckContext, CheckResult, failed


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Build the tree once per strategy and pass only if both builds count.

    Each build gets its own workspace on the builder, keyed by the region,
    the tree, and the strategy's name, so the two never read each other's
    object files and a later onboarding step can find either one again.

    The detail holds one entry per strategy -- the targets it built, the
    compiler command lines it ran, and, on a failure, which of the three
    statements did not hold.
    """
    manifest = ctx.provenance.manifest()
    tree = ctx.tree.payload()

    per_strategy = {
        one.name: build_replay.build_verdict(
            ctx.builder, ctx.provenance.attempt_id(one), tree, one, manifest,
        )
        for one in ctx.provenance.strategies()
    }

    did_not_build = [name for name, result in per_strategy.items() if result.verdict != "pass"]
    detail = {
        "strategies": {name: result.detail for name, result in per_strategy.items()},
        "failed_strategies": did_not_build,
        "targets_asked_for": [target["role"] for target in build_replay.build_targets(manifest)],
        "manifest_sha256": manifest.sha256,
    }
    if did_not_build:
        return failed(detail, [
            f"strategy '{name}': {reason}"
            for name in did_not_build for reason in per_strategy[name].reasons
        ])
    return CheckResult(verdict="pass", detail=detail)
