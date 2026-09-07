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
policy, read from the promoted manifest. The band comes from the policy's
`files` section, one entry per file the timing run writes, and not from
the `variables` section the oracle judges the region by: one call of a
region and a whole program run are different measurements, and a band
calibrated for one of them says nothing about the other.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from equivalent.capture import compare
from equivalent.tree import attempt_id_for
from equivalent.ledger.capture_sets import (
    PROGRAM_SET,
    program_arrays,
    program_variable,
)
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import Manifest

from .context import CheckContext, CheckResult
from .errors import ComponentError
# The band consulted here is the one per file the timing run writes. The
# region's own bands live beside it under `variables` and are not these:
# they band one call of the region, where a whole-program run accumulates
# the difference between two compilations over every step it takes.
from .names import FILE_BANDS, PROGRAM_SET_KEY
from .timing import timing_target

# The claim that leaves a reference behind.
BASELINE_PREDICATE = "timing/baseline"
# The action that files it, named in the error when there is none.
BASELINE_ACTION = "time_baseline"

# How many times the program is run here. One: this is a comparison, and
# how long the program takes is what `time_port` is for.
REPEATS = 1


def tolerance_policy(manifest: Manifest) -> tuple[dict, str]:
    """The code's band per timing output file, and the hash of the file they came from.

    The bytes hashed are the tolerance file's own, which is what the
    oracle hashes for the same file -- so the policy subject on this
    claim and the one on a regression claim are the same subject when
    they are the same policy.
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


def _reference_outputs(sets, sha256: str) -> dict:
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
    reference = _reference_outputs(sets, program_set)
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


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Run the port's own program and compare its files with the baseline's.

    The detail holds the per-output comparison, what the run cost, and the
    two things the verdict rests on -- the tolerance policy and the
    baseline's program capture set -- which come back as the claim's
    materials. Raises ComponentError when the set the baseline named is
    gone, or when the builder could not be reached.
    """
    manifest = ctx.manifest
    target = timing_target(manifest)
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
            attempt_id_for(ctx.region_id, ctx.tree.sha), target.executable, list(timing.args),
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
