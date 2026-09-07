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

What it compares against is the capture set the deployment's own
`time_baseline` run stored: the reference is a run of this deployment's
baseline, not a file checked in beside the code, because a real timing
size writes megabytes per run. That claim is filed against the baseline
tree, so the precondition table names it against that subject and the
gateway refuses the request, naming `time_baseline`, when it is not
there.

The comparison is the harness's one comparator (equivalent/capture/
compare.py, which the oracle also uses), under the code's own tolerance
policy, read from the promoted manifest. Which band that is, and how two
runs' files are compared under it, is program_outputs' -- the port's own
timing claim compares the same files the same way, and two comparators
would let a port pass one and fail the other.
"""
from __future__ import annotations

from equivalent.ledger.subjects import Subject

from .context import CheckContext, CheckResult
from .errors import ComponentError
from .names import PROGRAM_SET_KEY
from .program_outputs import (
    comparison_reasons,
    compare_outputs,
    timing_target,
    tolerance_policy,
)

# The claim that leaves a reference behind.
BASELINE_PREDICATE = "timing/baseline"
# The action that files it, named in the error when there is none.
BASELINE_ACTION = "time_baseline"

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
            f"is nothing to compare this port's program against; the baseline run of "
            f"{BASELINE_ACTION} left none"
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
    target, _ = timing_target(ctx, manifest)
    timing = manifest.timing
    bands, policy_sha256 = tolerance_policy(manifest)
    program_set = reference_set(ctx)
    rests_on = {"policy_sha256": policy_sha256, PROGRAM_SET_KEY: program_set}
    materials = (
        Subject(kind="policy", sha256=policy_sha256),
        Subject(kind="capture_set", sha256=program_set),
    )

    try:
        resp = ctx.builder.time(
            ctx.provenance.attempt_id(), target.executable, list(timing.args),
            dict(timing.env), list(timing.outputs), REPEATS, timing.budget_s,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc
    if not resp.get("ok"):
        # An exceeded budget and a declared file the program never wrote
        # both arrive this way, and the builder's own words say which.
        return CheckResult(
            verdict="fail",
            detail={**rests_on, "log_tail": resp.get("log_tail", "")},
            reasons=("the program did not finish inside its budget, or did not write "
                     "every file the manifest declares",),
            materials=materials,
        )

    runs = resp.get("outputs", [])
    last_run = runs[-1] if runs else {}
    per_var = compare_outputs(ctx.sets, program_set, manifest, last_run, bands)

    detail = {
        **rests_on, "per_var": per_var, "runs_s": resp.get("runs_s", []),
        "executable_identity": resp.get("executable_identity"),
    }
    if all(entry["pass"] for entry in per_var.values()):
        return CheckResult(verdict="pass", detail=detail, materials=materials)
    return CheckResult(
        verdict="fail", detail=detail, reasons=tuple(comparison_reasons(per_var)), materials=materials,
    )
