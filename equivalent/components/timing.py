"""Wraps the builder's /v1/time as two gateway components.

time_port times the region's own current tree. It relies on build_replay
having already built the timing binary in the same builder workspace --
build_replay sends the whole tree and asks make for every target the
manifest declares, so the program the timing run needs is already there.

time_baseline measures the pristine baseline instead, which the region's
own build_replay call never touches -- so this component does its own
build first, with the region's `baseline_strategy`: the comparison floor,
a strategy file like any other rather than a name inside the builder.
The resulting claim is filed against the baseline tree, not whatever tree
happens to be current for the region.

What is timed is the code's own program, at the size its manifest
declares: the executable, its arguments, its environment, and the files
it must write are all manifest fields, so changing the problem size is an
edit to data and not to a source file the agent can reach.
"""
from __future__ import annotations

import base64
import hashlib

from equivalent.gateway.submit import attempt_id_for, tree_payload
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy

from .build_replay import build_tree
from .errors import ComponentError

# The manifest role of the program a timing run measures.
TIMING_ROLE = "timing"


def _timing_target(manifest: Manifest):
    target = manifest.build.targets.get(TIMING_ROLE)
    if target is None:
        raise ComponentError(
            f"code '{manifest.name}' declares no '{TIMING_ROLE}' build target, so there "
            f"is no program to time"
        )
    return target


def _collected(runs: list) -> dict:
    """What the last run wrote, named and hashed rather than carried.

    The builder collects the declared files once per run; a timing claim
    describes the binary that finished, so it is the last run's files
    that are recorded. Whether every run wrote the same thing is an
    onboarding question, asked where the two are compared.

    The files themselves can be large and are the program's output, not
    evidence about it; their names and digests are what a reader needs to
    see that two runs produced the same thing.
    """
    last = runs[-1] if runs else {}
    return {
        name: hashlib.sha256(base64.b64decode(encoded)).hexdigest()
        for name, encoded in sorted(last.items())
    }


def _time(builder, attempt_id: str, manifest: Manifest, repeats: int,
          extra_detail: dict | None = None) -> dict:
    target = _timing_target(manifest)
    timing = manifest.timing
    try:
        resp = builder.time(
            attempt_id, target.executable, list(timing.args), dict(timing.env),
            list(timing.outputs), repeats, timing.budget_s,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc
    if not resp.get("ok"):
        return {"verdict": "fail", "detail": {"log_tail": resp.get("log_tail", "")}}
    detail = {
        "runs_s": resp["runs_s"],
        "gpu_exclusive": resp.get("gpu_exclusive"),
        # What was run, so a later reader can tell two timing claims apart
        # without going back to the manifest of the day.
        "executable": target.executable,
        "args": list(timing.args),
        "env": dict(timing.env),
        "outputs": _collected(resp.get("outputs", [])),
    }
    detail.update(extra_detail or {})
    return {"verdict": "pass", "detail": detail}


def check_port(store: LedgerStore, tree: Subject, region_id: str, tree_sha: str,
               manifest: Manifest, builder, repeats: int = 5) -> dict:
    """Time the port and record the flags it was actually built with.

    The flags come from the tree's own build/replay claim -- the builder's
    record of what it passed to the compiler -- not recomputed from the
    strategy, so the timing claim describes the binary that really exists.
    Same read-back pattern as regression_visible using gpu/executed's
    outputs.
    """
    build_claim = store.latest("build/replay", tree)
    if build_claim is None or build_claim.predicate.verdict != "pass":
        raise ComponentError("no passing build/replay claim for this tree")
    flags = build_claim.predicate.detail.get("flags")
    return _time(
        builder, attempt_id_for(region_id, tree_sha), manifest, repeats,
        extra_detail={"flags": flags},
    )


def check_baseline(
    repo_dir, region_id: str, baseline_tree_sha: str,
    manifest: Manifest, baseline_strategy: Strategy, builder, repeats: int = 5,
) -> dict:
    attempt_id = attempt_id_for(f"{region_id}-baseline", baseline_tree_sha)
    build_resp = build_tree(
        builder, attempt_id, tree_payload(repo_dir, "main"), baseline_strategy, manifest,
    )
    if not build_resp.get("ok"):
        return {
            "verdict": "fail",
            "detail": {
                "stage": "build", "strategy": baseline_strategy.name,
                "log_tail": build_resp.get("log_tail", ""),
            },
        }
    return _time(
        builder, attempt_id, manifest, repeats,
        extra_detail={"strategy": baseline_strategy.name, "flags": build_resp.get("flags")},
    )
