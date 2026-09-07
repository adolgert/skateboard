"""Shared build recipe, request, and verdict machinery.

These operations are neutral between the porting build action, onboarding's
two-strategy build, and the independently preserved original.
"""
from __future__ import annotations

from dataclasses import dataclass

from equivalent.ledger.artifacts import build_record
from equivalent.ledger.vocabulary import PASS, TARGETS_KEY
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy

from . import backend
from .errors import ComponentError
from .names import CAPTURE_ROLE, REPLAY_ROLE, TIMING_ROLE
from .result import CheckResult, failed


BUILD_ROLES = (REPLAY_ROLE, TIMING_ROLE, CAPTURE_ROLE)


def build_targets(manifest: Manifest) -> list[dict]:
    """The manifest's build targets as the builder's wire wants them."""
    return [
        {"role": role, "target": target.target, "executable": target.executable}
        for role in BUILD_ROLES
        if (target := manifest.build.targets.get(role)) is not None
    ]


@dataclass(frozen=True)
class Recipe:
    makefile: str
    targets: tuple
    source_patterns: tuple

    @classmethod
    def from_manifest(cls, manifest: Manifest) -> "Recipe":
        return cls(
            makefile=manifest.build.makefile,
            targets=tuple(build_targets(manifest)),
            source_patterns=tuple(manifest.source.patterns),
        )


def fortran_of(strategy: Strategy):
    fortran = strategy.languages.get("fortran")
    if fortran is None:
        raise ComponentError(f"strategy '{strategy.name}' defines no fortran language entry")
    return fortran


def build_tree(builder, attempt_id: str, tree: list[dict], strategy: Strategy,
               recipe: Recipe):
    """One /v1/build call, described entirely by the strategy and the recipe."""
    fortran = fortran_of(strategy)
    return backend.build(
        builder, attempt_id, tree, recipe.makefile, list(recipe.targets),
        fortran.compiler, fortran.flags, strategy.link_flags, recipe.source_patterns,
    )


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
                  recipe: Recipe) -> CheckResult:
    """Build one tree and judge the three statements every build must make."""
    resp = build_tree(builder, attempt_id, tree, strategy, recipe)
    try:
        records = (build_record(attempt_id, resp.targets),)
    except ValueError as exc:
        # A failed build ordinarily has no artifact identities: the builder
        # never froze partial output. A purportedly successful build without
        # readable identities is a broken backend answer, not a code verdict.
        if resp.ok:
            raise ComponentError(
                f"the builder's successful build answer has invalid artifact identities: {exc}"
            ) from exc
        records = ()
    declarations = {"build_records": records}
    common = {
        "attempt_id": attempt_id, "flags": resp.flags,
        TARGETS_KEY: resp.targets, "compiles": resp.compiles,
        "executor_identity": resp.executor_identity,
        "image_id": resp.image_id,
    }

    if not resp.ok:
        missing = resp.missing_targets or []
        return failed(
            {**common, "missing_targets": resp.missing_targets, "log_tail": resp.log_tail},
            ["the build did not finish",
             *(f"the build produced no '{target}'" for target in missing)],
            **declarations,
        )
    if not resp.flags_reached_every_compile:
        hint = ("the makefile compiled without the strategy's flags; it must pass "
                "FFLAGS through to every compile rather than setting its own")
        return failed(
            {**common, "compiles_without_flags": _without_flags(resp.compiles), "hint": hint},
            [hint], **declarations,
        )
    if not resp.compiled_only_tree_source:
        hint = ("the build compiled a file that is not this code's own source; "
                "every compiled file must be in the submitted tree and match the "
                "manifest's source patterns")
        return failed(
            {**common, "files_outside_tree": _outside_tree(resp.compiles), "hint": hint},
            [hint], **declarations,
        )
    return CheckResult(
        verdict=PASS,
        detail={**common, "minfo_excerpt": resp.minfo_excerpt, "log_tail": resp.log_tail},
        **declarations,
    )
