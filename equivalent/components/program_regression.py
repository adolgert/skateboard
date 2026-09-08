"""Runs the ported program at the size it is timed at and compares what it wrote.

Trust role: this is the check that a port is still the same code at the
size the timing claim measures. Every other regression check runs the
replay driver on captured cases of one region; this one runs the code's
own program, end to end, and compares the files it writes against the
files the baseline program wrote. A port that is fast because it computes
something else at scale is caught here and nowhere else, so a verdict
that passed on a comparison that did not really happen -- a missing file
read as nothing to check, a band nobody chose -- would be the whole of
what went wrong.

What it compares against is the capture set the deployment's own baseline
timing run stored: the reference is a run of this deployment's baseline,
not a file checked in beside the code, because a real timing size writes
megabytes per run. That claim is filed against the baseline tree, so the
precondition table names it against that subject and the gateway refuses
the request when it is not there.

The comparison is the harness's one comparator (equivalent/capture/
compare.py, which the oracle also uses), under the code's own tolerance
policy, read from the promoted manifest. Which band that is, how two
runs' files are compared under it, and what counts as a measurement at
all are program_outputs' -- the port's own timing claim runs the same
program the same way, and two readings of one answer would let a port
pass one and fail the other.
"""
from __future__ import annotations

from dataclasses import replace

from equivalent.ledger.artifacts import binary_artifacts
from equivalent.ledger.subjects import Subject
from equivalent.ledger.vocabulary import (
    EXECUTABLE_IDENTITY_KEY,
    FAIL,
    PASS,
    POLICY_KEY,
    PROGRAM_SET_KEY,
)

from .context import CheckContext
from .result import CheckResult
from .errors import ComponentError
from .names import TIMING_ROLE
from .program_outputs import (
    comparison_reasons,
    compare_outputs,
    time_program,
    tolerance_policy,
)

# The claim that leaves a reference behind.
BASELINE_PREDICATE = "timing/baseline"

# How many times the program is run here. One: this is a comparison, and
# how long the program takes is what `time_port` is for.
REPEATS = 1


def reference_set(ctx: CheckContext) -> str:
    """The program capture set the passing baseline timing left behind.

    The claim itself is a precondition of this action, so it is here; what
    it may still not have is a set, which is what a code that declares no
    timing outputs leaves. There is then nothing to compare against, and
    saying so is not a verdict about the port.
    """
    detail = ctx.claims[BASELINE_PREDICATE].predicate.detail
    if not detail.get(PROGRAM_SET_KEY):
        raise ComponentError(
            f"the passing {BASELINE_PREDICATE} claim stored no program outputs, so there "
            f"is nothing to compare this port's program against"
        )
    return detail[PROGRAM_SET_KEY]


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Run the port's own program and compare its files with the baseline's.

    The detail holds the per-output comparison, what the run cost, and the
    two things the verdict rests on -- the tolerance policy and the
    baseline's program capture set -- which come back as the claim's
    materials. Raises ComponentError when the set the baseline named is
    gone, or when the builder could not be reached.
    """
    manifest = ctx.provenance.manifest()
    per_file, policy = tolerance_policy(manifest)
    program_set = reference_set(ctx)
    rests_on = {POLICY_KEY: policy.sha256, PROGRAM_SET_KEY: program_set}
    materials = (policy, Subject(kind="capture_set", sha256=program_set))

    resp, refusal = time_program(
        ctx, ctx.provenance.attempt_id(), manifest, REPEATS, rests_on,
    )
    if refusal is not None:
        return replace(refusal, materials=materials)

    runs = resp.outputs
    per_var = compare_outputs(ctx.sets, program_set, manifest, runs[-1] if runs else {}, per_file)

    detail = {
        **rests_on, "per_var": per_var, "runs_s": resp.runs_s,
        EXECUTABLE_IDENTITY_KEY: resp.executable_identity,
    }
    artifacts = binary_artifacts(
        resp.executable_identity,
        executable=manifest.build.targets[TIMING_ROLE].executable,
    )
    if all(entry[PASS] for entry in per_var.values()):
        return CheckResult(
            verdict=PASS, detail=detail, materials=materials, binary_artifacts=artifacts,
        )
    return CheckResult(
        verdict=FAIL, detail=detail, reasons=tuple(comparison_reasons(per_var)),
        materials=materials, binary_artifacts=artifacts,
    )
