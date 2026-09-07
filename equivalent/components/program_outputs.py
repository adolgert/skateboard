"""Running the code's own program, and comparing the files it writes.

Trust role: three checks run the whole program at the size its manifest
declares -- the baseline timing that leaves the reference behind, the
onboarding timing that says the program is a reference at all, and the
port's own run that is compared against it -- and a fourth times the port.
Each of them rests on the same two statements: the builder's answer really
is a measurement, and the files the run wrote are the files the baseline
wrote. Written once here, those statements cannot be strict in one check
and lax in another; written four times they were.

The band consulted is the one per file the timing run writes. The
region's own bands live beside it under `variables` and are not these:
they band one call of the region, where a whole-program run accumulates
the difference between two compilations over every step it takes.

Reading what the builder hands back is here too, rather than beside the
sets in the ledger: the base64 of a file a program wrote is the
builder's wire, and the ledger's business is the arrays after somebody
has decided they are arrays.

This sits below `timing` and `program_regression` so that both can read
it without importing each other.
"""
from __future__ import annotations

import base64
import hashlib
import math
from pathlib import Path

from equivalent.capture import compare, npy
from equivalent.ledger.artifacts import binary_artifacts
from equivalent.ledger.capture_sets import PROGRAM_SET, pack_program_set
from equivalent.ledger.subjects import policy_subject
from equivalent.ledger.vocabulary import PASS
from equivalent.manifest.schema import Manifest

from . import backend
from .context import CheckContext
from .result import failed
from .errors import ComponentError
from .names import FILE_BANDS, TIMING_ROLE, bands

# What a manifest with no timing target leaves the harness without, in the
# words the message about it uses.
NO_PROGRAM_TO_TIME = "no program to time"


def timing_target(ctx: CheckContext, manifest: Manifest, described=None):
    """The program to time, or what to say when the manifest names none.

    Answers `(target, None)`, or `(None, verdict)` where the phase makes a
    missing target a verdict about the code rather than a fault on the
    harness's side. Which it is, and why, is the provenance's to say.
    """
    return ctx.provenance.build_target(manifest, TIMING_ROLE, NO_PROGRAM_TO_TIME, described)


def program_variable(path: str) -> str:
    """The variable a file a program wrote is stored under: its path without the suffix.

    A file in a directory of the program's own keeps that directory in
    its name, so two files called `rho.npy` in different directories stay
    two variables.
    """
    return path[: -len(npy.INPUT_SUFFIX)] if path.endswith(npy.INPUT_SUFFIX) else path


def program_arrays(written: dict, declared) -> tuple[dict, dict]:
    """The declared files one run wrote, as arrays, and a message per file that is not one.

    `written` is what the builder hands back for one run: {path: base64 of
    that file's bytes}. The program's outputs are compared as arrays, like
    the region's, so a file that is not an NPY file is a problem named
    here -- keyed by the path it came from, so a caller comparing one
    output at a time can say which one -- rather than a comparison that
    quietly did not happen.
    """
    arrays = {}
    problems = {}
    for path in declared:
        try:
            arrays[program_variable(path)] = npy.decode(base64.b64decode(written[path]))
        except Exception as exc:
            problems[path] = (
                f"the timing run's '{path}' does not read as an array ({exc}); the "
                f"program's outputs are compared as arrays, like the region's"
            )
    return arrays, problems


def packed_program_set(manifest: Manifest, resp) -> tuple:
    """What one timing run leaves for a port's own run to be compared against.

    Answers `(packed, problems)` for the last run's files: the set to
    keep, or why the program's declared outputs are not something a later
    run could be compared with. Both the baseline timing and the
    onboarding timing leave the same kind of reference behind, so both
    pack it here.
    """
    runs = resp.outputs
    arrays, unreadable = program_arrays(runs[-1] if runs else {}, manifest.timing.outputs)
    if unreadable:
        return None, sorted(unreadable.values())
    return pack_program_set(arrays), []


def collected(runs: list) -> dict:
    """What the last run wrote, named and hashed rather than carried.

    The builder collects the declared files once per run; a timing claim
    describes the binary that finished, so it is the last run's files that
    are recorded. Whether every run wrote the same thing is an onboarding
    question, asked where the two are compared.

    The files themselves can be large and are the program's output, not
    evidence about it; their names and digests are what a reader needs to
    see that two runs produced the same thing.
    """
    last = runs[-1] if runs else {}
    return {
        name: hashlib.sha256(base64.b64decode(encoded)).hexdigest()
        for name, encoded in sorted(last.items())
    }


def time_program(ctx: CheckContext, attempt_id: str, manifest: Manifest, repeats: int,
                 described=None) -> tuple:
    """Run the manifest's program `repeats` times and say if that is a measurement.

    Answers `(response, None)` when the builder's answer can be read as
    one: the run finished, every requested repetition came back with a
    positive finite duration, and every declared output came back from
    every one of them. Otherwise the second half is the `fail` verdict the
    caller should return, and the first is whatever the builder said. What
    the numbers mean is the caller's -- one check keeps the files as a
    reference, another compares them, a third only records the clock.

    Raises ComponentError if the builder could not be reached.
    """
    described = described or {}
    target, refusal = timing_target(ctx, manifest, described)
    if refusal is not None:
        return None, refusal
    timing = manifest.timing
    # The action table is the gate on a request's `repeats`, checked
    # before the gateway dispatches; the checks that pass their own
    # constant are the only other callers.
    if repeats < 1:
        raise ComponentError("timing repeats must be a positive whole number")
    resp = backend.time(
        ctx.builder, attempt_id, target.executable, timing.args, timing.env,
        timing.outputs, repeats, timing.budget_s,
    )

    if not resp.ok:
        # An exceeded budget and a declared file the program never wrote
        # both arrive this way, and the builder's own words say which.
        return resp, failed(
            {**described, "runs_s": resp.runs_s, "log_tail": resp.log_tail},
            ["the timed program did not finish inside its budget, or did not write every "
             "file the manifest declares"],
            binary_artifacts=binary_artifacts(
                resp.executable_identity, executable=target.executable,
            ),
        )

    durations = resp.runs_s
    runs = resp.outputs
    if (len(durations) != repeats
            or any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0
                   for t in durations)
            or len(runs) != repeats
            or any(not isinstance(run, dict) or set(timing.outputs) - run.keys()
                   for run in runs)):
        problems = ["timing requires every requested repetition, positive finite durations, "
                    "and every declared output"]
        return resp, failed(
            {**described, "problems": problems}, problems,
            binary_artifacts=binary_artifacts(
                resp.executable_identity, executable=target.executable,
            ),
        )
    return resp, None


def tolerance_policy(manifest: Manifest) -> tuple:
    """The code's band per timing output file, and the policy itself as a material.

    The subject is made from the tolerance file's own bytes, which is what
    the oracle hashes for the same file -- so the policy a program claim
    names and the one a regression claim names are the same subject when
    they are the same policy.
    """
    try:
        data = Path(manifest.tolerances).read_bytes()
        _, per_file = bands(data)
    except (OSError, ValueError) as exc:
        raise ComponentError(
            f"the code's tolerance policy at {manifest.tolerances} does not read as a "
            f"policy naming a band per file the timing run writes: {exc}"
        ) from exc
    return per_file, policy_subject(data)


def reference_outputs(sets, sha256: str) -> dict:
    try:
        return sets.load(sha256)[PROGRAM_SET]["outputs"]
    except (FileNotFoundError, KeyError) as exc:
        raise ComponentError(
            f"the program capture set {sha256} a baseline timing claim names is not in "
            f"this region's ledger: {exc}"
        ) from exc


def _compare_one(path: str, name: str, reference, written: dict, bands: dict) -> dict:
    """One declared output of the port's run against the baseline's."""
    if name not in written:
        return {
            PASS: False,
            "error": f"the timing run wrote no '{path}', which the manifest declares and "
                     f"the baseline program wrote",
        }
    band = bands.get(path)
    if reference.dtype.kind == "f" and band is None:
        return {
            PASS: False,
            "error": f"the timing output '{path}' holds floating-point numbers and the "
                     f"code's tolerance policy names no band for it under "
                     f"'{FILE_BANDS}', so how close is close enough is a question "
                     f"nobody has answered",
        }
    return compare.compare_variable(reference, written[name], band)


def compare_outputs(sets, program_set, manifest, encoded_outputs, per_file):
    """Compare one measured repetition with the reviewed baseline outputs."""
    reference = reference_outputs(sets, program_set)
    written, unreadable = program_arrays(
        encoded_outputs, [path for path in manifest.timing.outputs if path in encoded_outputs],
    )
    per_var = {}
    for path in manifest.timing.outputs:
        name = program_variable(path)
        if path in unreadable:
            per_var[name] = {PASS: False, "error": unreadable[path]}
        elif name not in reference:
            per_var[name] = {PASS: False, "error": f"baseline capture set holds no '{name}'"}
        else:
            per_var[name] = _compare_one(path, name, reference[name], written, per_file)
    return per_var


def comparison_reasons(per_var: dict) -> list:
    """Why a comparison failed, one line per output that did not match."""
    return [
        f"the timing run's '{name}': {entry.get('error', 'is not the baseline program\'s')}"
        for name, entry in sorted(per_var.items()) if not entry[PASS]
    ]
