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

from equivalent.gateway.submit import attempt_id_for_strategy, tree_payload
from equivalent.strategy.schema import Strategy

from . import build_replay, tree_manifest


def check(
    repo_dir, ref: str, region_id: str, tree_sha: str,
    strategy: Strategy, baseline_strategy: Strategy, builder,
) -> dict:
    """Build the tree once per strategy and pass only if both builds count.

    Each build gets its own workspace on the builder, keyed by the region,
    the tree, and the strategy's name, so the two never read each other's
    object files and a later onboarding step can find either one again.

    Returns {"verdict": "pass" | "fail", "detail": {...}}, where the detail
    holds one entry per strategy -- the targets it built, the compiler
    command lines it ran, and, on a failure, which of the three statements
    did not hold.
    """
    manifest = tree_manifest.manifest_of(repo_dir, ref)
    tree = tree_payload(repo_dir, ref)

    per_strategy = {}
    for one in (baseline_strategy, strategy):
        attempt_id = attempt_id_for_strategy(region_id, tree_sha, one.name)
        per_strategy[one.name] = build_replay.build_verdict(
            builder, attempt_id, tree, one, manifest,
        )

    failed = [name for name, result in per_strategy.items() if result["verdict"] != "pass"]
    detail = {
        "strategies": {name: result["detail"] for name, result in per_strategy.items()},
        "failed_strategies": failed,
        "targets_asked_for": [target["role"] for target in build_replay.build_targets(manifest)],
        "manifest_sha256": manifest.sha256,
    }
    return {"verdict": "fail" if failed else "pass", "detail": detail}
