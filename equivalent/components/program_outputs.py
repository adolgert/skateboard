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

This sits below `timing` and `program_regression` so that both can read
it without importing each other.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path

from equivalent.capture import compare
from equivalent.ledger.capture_sets import (
    PROGRAM_SET,
    program_arrays,
    program_variable,
)
from equivalent.manifest.schema import Manifest

from .context import CheckContext, failed
from .errors import ComponentError
from .names import FILE_BANDS, TIMING_ROLE

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
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ComponentError("timing repeats must be an integer between 1 and 100")
    try:
        resp = ctx.builder.time(
            attempt_id, target.executable, list(timing.args), dict(timing.env),
            list(timing.outputs), repeats, timing.budget_s,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc

    if not resp.get("ok"):
        # An exceeded budget and a declared file the program never wrote
        # both arrive this way, and the builder's own words say which.
        return resp, failed(
            {**described, "runs_s": resp.get("runs_s", []),
             "log_tail": resp.get("log_tail", "")},
            ["the timed program did not finish inside its budget, or did not write every "
             "file the manifest declares"],
        )

    durations = resp.get("runs_s", [])
    runs = resp.get("outputs", [])
    if (not isinstance(durations, list) or len(durations) != repeats
            or any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0
                   for t in durations)
            or not isinstance(runs, list) or len(runs) != repeats
            or any(not isinstance(run, dict) or set(timing.outputs) - run.keys()
                   for run in runs)):
        problems = ["timing requires every requested repetition, positive finite durations, "
                    "and every declared output"]
        return resp, failed({**described, "problems": problems}, problems)
    return resp, None


def tolerance_policy(manifest: Manifest) -> tuple[dict, str]:
    """The code's band per timing output file, and the hash of the file they came from.

    The bytes hashed are the tolerance file's own, which is what the
    oracle hashes for the same file -- so the policy subject on a program
    claim and the one on a regression claim are the same subject when they
    are the same policy.
    """
    try:
        data = Path(manifest.tolerances).read_bytes()
        bands = json.loads(data).get(FILE_BANDS, {})
        if not isinstance(bands, dict):
            raise TypeError(f"'{FILE_BANDS}' is not a mapping")
    except (OSError, ValueError, TypeError) as exc:
        raise ComponentError(
            f"the code's tolerance policy at {manifest.tolerances} does not read as a "
            f"policy naming a band per file the timing run writes: {exc}"
        ) from exc
    return bands, hashlib.sha256(data).hexdigest()


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
            "pass": False,
            "error": f"the timing run wrote no '{path}', which the manifest declares and "
                     f"the baseline program wrote",
        }
    band = bands.get(path)
    if reference.dtype.kind == "f" and band is None:
        return {
            "pass": False,
            "error": f"the timing output '{path}' holds floating-point numbers and the "
                     f"code's tolerance policy names no band for it under "
                     f"'{FILE_BANDS}', so how close is close enough is a question "
                     f"nobody has answered",
        }
    return compare.compare_variable(reference, written[name], band)


def compare_outputs(sets, program_set, manifest, encoded_outputs, bands):
    """Compare one measured repetition with the reviewed baseline outputs."""
    reference = reference_outputs(sets, program_set)
    written, unreadable = program_arrays(
        encoded_outputs, [path for path in manifest.timing.outputs if path in encoded_outputs],
    )
    per_var = {}
    for path in manifest.timing.outputs:
        name = program_variable(path)
        if path in unreadable:
            per_var[name] = {"pass": False, "error": unreadable[path]}
        elif name not in reference:
            per_var[name] = {"pass": False, "error": f"baseline capture set holds no '{name}'"}
        else:
            per_var[name] = _compare_one(path, name, reference[name], written, bands)
    return per_var


def comparison_reasons(per_var: dict) -> list:
    """Why a comparison failed, one line per output that did not match."""
    return [
        f"the timing run's '{name}': {entry.get('error', 'is not the baseline program\'s')}"
        for name, entry in sorted(per_var.items()) if not entry["pass"]
    ]
