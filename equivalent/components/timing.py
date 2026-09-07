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

time_baseline also keeps what the baseline program wrote, as a capture
set of one case whose variables are the files themselves. That set is the
reference a port's own program run is later compared against, and it is a
run of this deployment's baseline rather than anything checked in: a
program at a real timing size writes megabytes every run.
"""
from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import replace

from equivalent.ledger.capture_sets import pack_program_set, program_arrays
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import Manifest
from equivalent.tree import attempt_id_for

from .build_replay import build_verdict
from .context import CheckContext, CheckResult, failed
from .errors import ComponentError
from .names import PROGRAM_SET_KEY, TIMING_ROLE

# The claims a port's timing rests on: what the binary was built with, and
# the program comparison that says the port is still the same code at this
# size.
BUILD_PREDICATE = "build/replay"
PROGRAM_PREDICATE = "program/regression"
# Which of the request's subjects a baseline timing is filed against.
BASELINE_SUBJECT = "baseline_tree"
# How many timed runs a request that says nothing asks for.
DEFAULT_REPEATS = 5
# What the baseline claim's detail says instead when there was nothing
# to store.
PROGRAM_SET_ABSENT = "program_set_absent"


def timing_target(manifest: Manifest):
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
          extra_detail: dict | None = None) -> tuple[CheckResult, dict]:
    """The result to file, and the builder's own answer it was made from.

    The answer is handed back too because the baseline does one more thing
    with it than the claim's detail records: it keeps the files the last
    run wrote.
    """
    target = timing_target(manifest)
    timing = manifest.timing
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ComponentError("timing repeats must be an integer between 1 and 100")
    try:
        resp = builder.time(
            attempt_id, target.executable, list(timing.args), dict(timing.env),
            list(timing.outputs), repeats, timing.budget_s,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc
    if not resp.get("ok"):
        return failed(
            {"log_tail": resp.get("log_tail", "")},
            ["the timed program did not finish inside its budget, or did not write every "
             "file the manifest declares"],
        ), resp
    durations = resp.get("runs_s", [])
    runs = resp.get("outputs", [])
    if (not isinstance(durations, list) or len(durations) != repeats
            or any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in durations)
            or not isinstance(runs, list) or len(runs) != repeats
            or any(not isinstance(run, dict) or set(timing.outputs) - run.keys() for run in runs)):
        problems = ["timing requires every requested repetition, positive finite durations, and every declared output"]
        return failed({"problems": problems}, problems), resp
    detail = {
        "runs_s": resp["runs_s"],
        "gpu_exclusive": resp.get("gpu_exclusive"),
        # What was run, so a later reader can tell two timing claims apart
        # without going back to the manifest of the day.
        "executable": target.executable,
        "args": list(timing.args),
        "env": dict(timing.env),
        "outputs": _collected(resp.get("outputs", [])),
        "executable_identity": resp.get("executable_identity"),
    }
    detail.update(extra_detail or {})
    return CheckResult(verdict="pass", detail=detail), resp


def check_port(ctx: CheckContext, config: dict) -> CheckResult:
    """Time the port and record the flags it was actually built with.

    The flags come from the tree's own build claim -- the builder's record
    of what it passed to the compiler -- not recomputed from the strategy,
    so the timing claim describes the binary that really exists. Same
    read-back pattern as regression_visible using gpu/executed's outputs.
    """
    from . import program_regression

    flags = ctx.claims[BUILD_PREDICATE].predicate.detail.get("flags")
    program_claim = ctx.claims[PROGRAM_PREDICATE]
    program_set = program_claim.predicate.detail.get(PROGRAM_SET_KEY)
    if not program_set:
        raise ComponentError(
            f"the passing {PROGRAM_PREDICATE} claim names no baseline output reference"
        )
    result, response = _time(
        ctx.builder, attempt_id_for(ctx.region_id, ctx.tree.sha), ctx.manifest,
        int(config.get("repeats", DEFAULT_REPEATS)), extra_detail={"flags": flags},
    )
    if result.verdict != "pass":
        return result

    bands, policy_sha = program_regression.tolerance_policy(ctx.manifest)
    comparisons = [
        program_regression.compare_outputs(ctx.sets, program_set, ctx.manifest, run, bands)
        for run in response["outputs"]
    ]
    detail = {
        **result.detail, PROGRAM_SET_KEY: program_set, "policy_sha256": policy_sha,
        "compared_repetitions": len(comparisons), "per_run": comparisons,
    }
    reasons = [
        reason for per_var in comparisons for reason in program_regression.comparison_reasons(per_var)
    ]
    if all(per_var and all(v["pass"] for v in per_var.values()) for per_var in comparisons):
        return CheckResult(verdict="pass", detail=detail)
    return failed(detail, reasons or [
        "a timed repetition wrote nothing the baseline program's outputs could be "
        "compared with"
    ])


def check_baseline(ctx: CheckContext, config: dict) -> CheckResult:
    """Build and time the pristine baseline, and keep what its program wrote.

    The files the last run wrote are packed as a capture set of one case
    named by the files themselves -- `h.npy` becomes the variable `h` --
    and the claim's detail names the set. That set is the reference
    program_regression compares a port's own program run against, so this
    claim is not only a measurement: it is where the reference comes from.
    A code whose manifest declares no timing outputs leaves none, and the
    detail says so rather than being silent about it.

    The claim is filed against the baseline tree, not whatever tree
    happens to be current for the region.
    """
    baseline_strategy = ctx.baseline_strategy
    attempt_id = attempt_id_for(f"{ctx.region_id}-baseline", ctx.baseline.sha)
    build_result = build_verdict(
        ctx.builder, attempt_id, ctx.baseline.payload(), baseline_strategy, ctx.manifest,
    )
    if build_result.verdict != "pass":
        return CheckResult(
            verdict="fail",
            detail={
                "stage": "build", "strategy": baseline_strategy.name,
                "build": build_result.detail,
            },
            reasons=build_result.reasons,
            subject_kind=BASELINE_SUBJECT,
        )
    result, resp = _time(
        ctx.builder, attempt_id, ctx.manifest, int(config.get("repeats", DEFAULT_REPEATS)),
        extra_detail={"strategy": baseline_strategy.name,
                      "flags": build_result.detail.get("flags"),
                      "build": build_result.detail},
    )
    if result.verdict != "pass":
        return replace(result, subject_kind=BASELINE_SUBJECT)
    stored, packed, problems = _packed_program(ctx.manifest, resp)
    detail = {**result.detail, **stored}
    # The program set this run stores is what a port's own program run is
    # compared against, so it is a formal material rather than a note in
    # the detail.
    materials = (
        (Subject(kind="capture_set", sha256=packed.sha256),) if packed is not None else ()
    )
    return CheckResult(
        verdict="fail" if problems else "pass",
        detail=detail,
        reasons=tuple(problems),
        materials=materials,
        stores=() if packed is None else (packed,),
        subject_kind=BASELINE_SUBJECT,
    )


def _packed_program(manifest: Manifest, resp: dict) -> tuple[dict, object, list]:
    """What this run leaves as a reference, the set itself, and what is wrong.

    A code that declares no timing outputs leaves no reference and is
    still a measurement. A code whose declared outputs are not arrays is
    not: the onboarding checks refuse such a program, so reaching here
    means the manifest changed under a code that was already brought in,
    and the claim would otherwise read as a baseline a port could be
    compared against.
    """
    declared = manifest.timing.outputs
    if not declared:
        return {
            PROGRAM_SET_KEY: None,
            PROGRAM_SET_ABSENT: "the manifest declares no timing outputs, so this run "
                                "left nothing a port's own program run could be "
                                "compared against",
        }, None, []
    runs = resp.get("outputs", [])
    arrays, unreadable = program_arrays(runs[-1] if runs else {}, declared)
    if unreadable:
        problems = sorted(unreadable.values())
        return {PROGRAM_SET_KEY: None, "problems": problems}, None, problems
    packed = pack_program_set(arrays)
    return {PROGRAM_SET_KEY: packed.sha256}, packed, []
