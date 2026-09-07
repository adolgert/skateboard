"""Times the code's own program twice and keeps the files it wrote.

Trust role: what this returns becomes a claim, and the capture set it
stores is the answer a ported program's whole-program run is later
compared against. Two things have to hold, and both are about the
program rather than about the clock. It has to finish inside the budget
its manifest declares and write every file that manifest declares --
otherwise there is no measurement and nothing to compare. And it has to
write the same files twice: a program whose output drifts from run to
run cannot be the reference for anything, and the drift is far easier to
see now, on the baseline, than later in a port's failing comparison.

What is timed is the code's own program at the size its manifest
declares -- the executable, its arguments, its environment, the files it
writes, and the budget are all manifest fields -- built the way the
baseline is built. Running it and reading the builder's answer is
program_outputs' job, the same one a port's timing goes through, so
"there was a measurement" cannot mean one thing here and another there.
The timings themselves are recorded for a reader; nothing here judges
them. How fast a port has to be is a question for the port.

The last run's files are stored as a capture set of one case, whose
variables are the files themselves: `h.npy` is stored as the variable
`h`, so the program's outputs are compared later by the same comparator
that compares the region's. The claim names that set under the same key
a baseline timing claim names its own under, because they are the same
thing said about two different programs.
"""
from __future__ import annotations

from equivalent.ledger.subjects import Subject
from equivalent.ledger.vocabulary import EXECUTABLE_IDENTITY_KEY, PASS, PROGRAM_SET_KEY

from . import program_outputs
from .context import CheckContext, CheckResult, failed

# How many times the program is run. Two is what the question needs: one
# run to measure and a second to disagree with it.
REPEATS = 2


def _drifted(first: dict, second: dict, declared) -> list:
    """The declared files the two runs did not write identically."""
    problems = []
    for path in declared:
        if first.get(path) != second.get(path):
            problems.append(
                f"the timing run wrote a different '{path}' the second time; a program "
                f"whose outputs drift from run to run cannot be a reference"
            )
    return problems


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Run the timing program twice and pack what its last run wrote.

    The detail holds the two runs' wall-clock seconds, whether the GPU was
    to itself, what the last run wrote and what the manifest said it would
    write, and the capture set the ledger will hold the files under. The
    two are recorded separately, and `outputs` means here what it means in
    a port's own timing claim: the files that were written, named and
    hashed. A manifest that names no timing program is a `fail` here
    rather than an error, because the manifest is the agent's own work
    while a code is being brought in. Raises ComponentError if the builder
    could not be reached.
    """
    manifest = ctx.provenance.manifest()
    described = {"manifest_sha256": manifest.sha256}
    timing = manifest.timing

    resp, refusal = program_outputs.time_program(
        ctx, ctx.provenance.attempt_id(), manifest, REPEATS, described,
    )
    if refusal is not None:
        return refusal

    runs = resp.outputs
    measured = {
        **described,
        "runs_s": resp.runs_s,
        "gpu_exclusive": resp.gpu_exclusive,
        "outputs": program_outputs.collected(runs),
        "declared_outputs": list(timing.outputs),
        EXECUTABLE_IDENTITY_KEY: resp.executable_identity,
    }

    packed, unreadable = program_outputs.packed_program_set(manifest, resp)
    problems = [*_drifted(runs[0], runs[1], timing.outputs), *unreadable]
    if problems:
        return failed({**measured, "problems": problems}, problems)

    return CheckResult(
        verdict=PASS, detail={**measured, PROGRAM_SET_KEY: packed.sha256},
        materials=(Subject(kind="capture_set", sha256=packed.sha256),),
        stores=(packed,),
    )
