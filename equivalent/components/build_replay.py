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

from equivalent.manifest.schema import Manifest, source_files
from equivalent.strategy.schema import Strategy

from .context import CheckContext, CheckResult, failed
from .errors import ComponentError

# The targets the builder is asked for, in the order it asks. `replay` is
# what every regression check runs and every manifest must offer; the
# other two are built when the code declares them, so that a tree which
# no longer builds its own program or its own capture program says so at
# the build step rather than three actions later.
BUILD_ROLES = ("replay", "timing", "capture")


def build_targets(manifest: Manifest) -> list[dict]:
    """The manifest's build targets as the builder's wire wants them."""
    return [
        {"role": role, "target": target.target, "executable": target.executable}
        for role in BUILD_ROLES
        if (target := manifest.build.targets.get(role)) is not None
    ]


def fortran_of(strategy: Strategy):
    fortran = strategy.languages.get("fortran")
    if fortran is None:
        raise ComponentError(f"strategy '{strategy.name}' defines no fortran language entry")
    return fortran


def build_tree(builder, attempt_id: str, tree: list[dict], strategy: Strategy,
               manifest: Manifest) -> dict:
    """One /v1/build call, described entirely by the strategy and the manifest."""
    fortran = fortran_of(strategy)
    try:
        return builder.build(
            attempt_id, tree, manifest.build.makefile, build_targets(manifest),
            fortran.compiler, list(fortran.flags), list(strategy.link_flags),
            list(manifest.source.patterns),
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/build call failed: {exc}") from exc


def _without_flags(compiles) -> list:
    return [record["argv"] for record in compiles if not record.get("has_flags")]


def _outside_tree(compiles) -> list:
    seen = []
    for record in compiles:
        for path in record.get("outside", ()):
            if path not in seen:
                seen.append(path)
    return seen


def build_verdict(builder, attempt_id: str, tree: list[dict], strategy: Strategy,
                  manifest: Manifest) -> CheckResult:
    """One tree, one strategy: build it and say whether that build counts.

    Three statements have to hold, and each is a verdict about the code
    rather than an error: the build succeeded, the strategy's flags
    reached every compile, and every file compiled was the tree's own
    source. This is the whole of what a build claim means, so both the
    porting check below and onboarding's two-strategy check call it
    rather than each deciding for itself.
    """
    resp = build_tree(builder, attempt_id, tree, strategy, manifest)

    compiles = resp.get("compiles", [])
    common = {
        "attempt_id": attempt_id, "flags": resp.get("flags"),
        "targets": resp.get("targets"), "compiles": compiles,
        "executor_identity": resp.get("executor_identity"),
        "image_id": resp.get("image_id"),
    }

    if not resp.get("ok"):
        missing = resp.get("missing_targets") or []
        return failed(
            {
                **common, "stage": resp.get("stage"),
                "missing_targets": resp.get("missing_targets"),
                "log_tail": resp.get("log_tail", ""),
            },
            [f"the build did not finish at stage {resp.get('stage')!r}",
             *(f"the build produced no '{target}'" for target in missing)],
        )

    if not resp.get("flags_reached_every_compile"):
        hint = ("the makefile compiled without the strategy's flags; it must pass "
                "FFLAGS through to every compile rather than setting its own")
        return failed(
            {**common, "compiles_without_flags": _without_flags(compiles), "hint": hint},
            [hint],
        )

    if not resp.get("compiled_only_tree_source"):
        hint = ("the build compiled a file that is not this code's own source; "
                "every compiled file must be in the submitted tree and match the "
                "manifest's source patterns")
        return failed(
            {**common, "files_outside_tree": _outside_tree(compiles), "hint": hint},
            [hint],
        )

    return CheckResult(
        verdict="pass",
        detail={
            **common, "minfo_excerpt": resp.get("minfo_excerpt", ""),
            "log_tail": resp.get("log_tail", ""),
        },
    )


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
        strategy, manifest,
    )
