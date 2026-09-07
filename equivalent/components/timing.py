"""Wraps the builder's /v1/time as two gateway components.

check_port times the region's own current tree. It relies on build_replay
having already built the timing binary in the same builder workspace --
build_replay sends the whole tree and asks make for every target the
manifest declares, so the program the timing run needs is already there.

check_baseline measures the pristine baseline instead, which the region's
own build_replay call never touches -- so this component does its own
build first, with the region's `baseline_strategy`: the comparison floor,
a strategy file like any other rather than a name inside the builder.
The resulting claim is filed against the baseline tree, not whatever tree
happens to be current for the region.

What is timed is the code's own program, at the size its manifest
declares: the executable, its arguments, its environment, and the files
it must write are all manifest fields, so changing the problem size is an
edit to data and not to a source file the agent can reach. Running it and
reading the builder's answer is program_outputs' job, which is also the
onboarding timing's, so the two cannot judge a measurement differently.

check_baseline also keeps what the baseline program wrote, as a capture
set of one case whose variables are the files themselves. That set is the
reference a port's own program run is later compared against, and it is a
run of this deployment's baseline rather than anything checked in: a
program at a real timing size writes megabytes every run.
"""
from __future__ import annotations

from dataclasses import replace

from equivalent.ledger.capture_sets import pack_program_set, program_arrays
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import Manifest
from equivalent.tree import attempt_id_for

from . import program_outputs
from .build_replay import build_verdict
from .context import CheckContext, CheckResult, failed
from .errors import ComponentError
from .names import PROGRAM_SET_KEY, TIMING_ROLE

# The other claim a port's timing rests on: the program comparison that
# says the port is still the same code at this size. Which claim carries
# the build it was compiled by is the provenance's to name.
PROGRAM_PREDICATE = "program/regression"
# Which of the request's subjects a baseline timing is filed against.
BASELINE_SUBJECT = "baseline_tree"
# How many timed runs a request that says nothing asks for.
DEFAULT_REPEATS = 5
# What the baseline claim's detail says instead when there was nothing
# to store.
PROGRAM_SET_ABSENT = "program_set_absent"


def _measured(resp, manifest: Manifest, extra: dict) -> dict:
    """The part of a timing claim's detail both of these record.

    The timing target is read straight from the manifest: a measurement
    only comes back at all once the run this describes has happened, and
    that run is of the program named here.
    """
    timing = manifest.timing
    return {
        "runs_s": resp.runs_s,
        "gpu_exclusive": resp.gpu_exclusive,
        # What was run, so a later reader can tell two timing claims apart
        # without going back to the manifest of the day.
        "executable": manifest.build.targets[TIMING_ROLE].executable,
        "args": list(timing.args),
        "env": dict(timing.env),
        "outputs": program_outputs.collected(resp.outputs),
        "executable_identity": resp.executable_identity,
        **extra,
    }


def check_port(ctx: CheckContext, config: dict) -> CheckResult:
    """Time the port and record the flags it was actually built with.

    The flags come from the tree's own build claim -- the builder's record
    of what it passed to the compiler -- not recomputed from the strategy,
    so the timing claim describes the binary that really exists. Same
    read-back pattern as regression_visible using gpu/executed's outputs.

    Every repetition's files are compared with the baseline program's, not
    only the one program_regression already compared: a port whose answers
    drift between runs at timing size is a port whose measured time is of
    something other than the code, and this is the only check that runs the
    program more than once.
    """
    manifest = ctx.provenance.manifest()
    flags = ctx.claims[ctx.provenance.build_predicate].predicate.detail.get("flags")
    program_claim = ctx.claims[PROGRAM_PREDICATE]
    program_set = program_claim.predicate.detail.get(PROGRAM_SET_KEY)
    if not program_set:
        raise ComponentError(
            f"the passing {PROGRAM_PREDICATE} claim names no baseline output reference"
        )
    response, refusal = program_outputs.time_program(
        ctx, ctx.provenance.attempt_id(), manifest,
        int(config.get("repeats", DEFAULT_REPEATS)),
    )
    if refusal is not None:
        return refusal

    bands, policy_sha = program_outputs.tolerance_policy(manifest)
    comparisons = [
        program_outputs.compare_outputs(ctx.sets, program_set, manifest, run, bands)
        for run in response.outputs
    ]
    detail = {
        **_measured(response, manifest, {"flags": flags}),
        PROGRAM_SET_KEY: program_set, "policy_sha256": policy_sha,
        "compared_repetitions": len(comparisons), "per_run": comparisons,
    }
    reasons = [
        reason for per_var in comparisons
        for reason in program_outputs.comparison_reasons(per_var)
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
    manifest = ctx.provenance.manifest()
    baseline_strategy = ctx.baseline_strategy
    # Not the provenance's workspace: what is built and timed here is the
    # pristine baseline, which the region's own build never touches.
    attempt_id = attempt_id_for(f"{ctx.region_id}-baseline", ctx.baseline.sha)
    build_result = build_verdict(
        ctx.builder, attempt_id, ctx.baseline.payload(), baseline_strategy, manifest,
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
    resp, refusal = program_outputs.time_program(
        ctx, attempt_id, manifest, int(config.get("repeats", DEFAULT_REPEATS)),
    )
    if refusal is not None:
        return replace(refusal, subject_kind=BASELINE_SUBJECT)
    stored, packed, problems = _packed_program(manifest, resp)
    detail = {
        **_measured(resp, manifest, {
            "strategy": baseline_strategy.name,
            "flags": build_result.detail.get("flags"),
            "build": build_result.detail,
        }),
        **stored,
    }
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


def _packed_program(manifest: Manifest, resp) -> tuple[dict, object, list]:
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
    runs = resp.outputs
    arrays, unreadable = program_arrays(runs[-1] if runs else {}, declared)
    if unreadable:
        problems = sorted(unreadable.values())
        return {PROGRAM_SET_KEY: None, "problems": problems}, None, problems
    packed = pack_program_set(arrays)
    return {PROGRAM_SET_KEY: packed.sha256}, packed, []
