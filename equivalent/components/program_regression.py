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
tree, and the precondition table can only require a claim on the subject
the action is about -- the port's tree -- so the requirement that the
baseline has been timed is checked here, in the component, and reported
as an error telling the session to run `time_baseline` first.

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
from equivalent.gateway.submit import attempt_id_for
from equivalent.ledger.capture_sets import (
    PROGRAM_SET,
    load_capture_set,
    program_arrays,
    program_variable,
)
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import Manifest

from .errors import ComponentError
from .timing import timing_target

# Where the tolerance policy keeps a band per file the timing run writes,
# keyed by the path the manifest declares. The region's own bands live
# beside it under `variables` and are not these: they band one call of
# the region, where a whole-program run accumulates the difference
# between two compilations over every step it takes.
FILE_BANDS = "files"

# The claim that leaves a reference behind, and what its detail calls the
# set it stored.
BASELINE_PREDICATE = "timing/baseline"
PROGRAM_SET_KEY = "program_set"
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


def reference_set(store: LedgerStore, baseline_tree: Subject) -> str:
    """The program capture set the latest passing baseline timing left behind."""
    stored = [
        claim.predicate.detail[PROGRAM_SET_KEY]
        for claim in store.claims_for(baseline_tree)
        if claim.predicateType == BASELINE_PREDICATE
        and claim.predicate.verdict == "pass"
        and claim.predicate.detail.get(PROGRAM_SET_KEY)
    ]
    if not stored:
        raise ComponentError(
            f"the baseline tree {baseline_tree.sha256} has no passing "
            f"{BASELINE_PREDICATE} claim that stored the program's outputs, so there is "
            f"nothing to compare this port's program against; run {BASELINE_ACTION} first"
        )
    return stored[-1]


def _reference_outputs(store: LedgerStore, sha256: str) -> dict:
    try:
        return load_capture_set(store, sha256)[PROGRAM_SET]["outputs"]
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


def check(store: LedgerStore, baseline_tree: Subject, region_id: str, tree_sha: str,
          manifest: Manifest, builder) -> dict:
    """Run the port's own program and compare its files with the baseline's.

    Returns {"verdict": "pass" | "fail", "detail": {...}}: the per-output
    comparison, what the run cost, and the two things the verdict rests on
    -- the tolerance policy and the baseline's program capture set --
    which the caller files as the claim's materials. Raises ComponentError
    when the baseline has not been timed, when the set it named is gone,
    or when the builder could not be reached.
    """
    target = timing_target(manifest)
    timing = manifest.timing
    bands, policy_sha256 = tolerance_policy(manifest)
    program_set = reference_set(store, baseline_tree)
    reference = _reference_outputs(store, program_set)
    rests_on = {"policy_sha256": policy_sha256, PROGRAM_SET_KEY: program_set}

    try:
        resp = builder.time(
            attempt_id_for(region_id, tree_sha), target.executable, list(timing.args),
            dict(timing.env), list(timing.outputs), REPEATS, timing.budget_s,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc
    if not resp.get("ok"):
        # An exceeded budget and a declared file the program never wrote
        # both arrive this way, and the builder's own words say which.
        return {
            "verdict": "fail",
            "detail": {**rests_on, "log_tail": resp.get("log_tail", "")},
        }

    runs = resp.get("outputs", [])
    last_run = runs[-1] if runs else {}
    # Only the files that are there are decoded: one the run never wrote
    # is a missing output, which is a different thing from a file that is
    # there and is not an array.
    written, unreadable = program_arrays(
        last_run, [path for path in timing.outputs if path in last_run],
    )
    per_var = {}
    for path in timing.outputs:
        name = program_variable(path)
        if path in unreadable:
            per_var[name] = {"pass": False, "error": unreadable[path]}
        elif name not in reference:
            per_var[name] = {
                "pass": False,
                "error": f"the baseline's program capture set holds no '{name}'; it was "
                         f"stored before the manifest declared this output",
            }
        else:
            per_var[name] = _compare_one(path, name, reference[name], written, bands)

    return {
        "verdict": "pass" if all(entry["pass"] for entry in per_var.values()) else "fail",
        "detail": {**rests_on, "per_var": per_var, "runs_s": resp.get("runs_s", [])},
    }
