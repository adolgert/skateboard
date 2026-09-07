"""Replays the captured inputs and requires the captured outputs back, bitwise.

Trust role: what this returns becomes a claim, and it is the claim that
says the replay driver and the capture program describe the same region.
Everything a port is judged by rests on that. If the driver read its
inputs in a different order than the capture program wrote them, or
called the region with an argument the capture program set differently,
every later comparison would be against answers that the baseline itself
does not produce -- and a correct port would look wrong, or a wrong one
right, for reasons no one could see in the numbers.

The comparison is exact. A tolerance band is for judging a port built
with different flags on different hardware; the driver and the capture
program are two halves of one harness, running the same code on the same
machine, and a difference between them is a mistake in the harness
rather than a difference in arithmetic.

The replay runs in the baseline strategy's workspace, which is the build
the capture program's own answers came from. No device proof is asked
for: whether anything offloads is a question for a port, not for the
harness around it.
"""
from __future__ import annotations

import base64

import numpy as np

from equivalent.capture import npy

from equivalent.ledger.vocabulary import (
    CAPTURE_SET_KEY,
    EXECUTABLE_IDENTITY_KEY,
    FAIL,
    PASS,
)
from . import backend, harness_capture
from .context import CheckContext, CheckResult, capture_set_materials
from .names import REPLAY_ROLE


def wire_inputs(cases: dict) -> dict:
    """A stored set's inputs as the builder's /v1/run wants them."""
    return {
        name: {
            variable: base64.b64encode(npy.encode(array)).decode()
            for variable, array in case["inputs"].items()
        }
        for name, case in cases.items()
    }


def difference(expected, got) -> dict | None:
    """How two arrays disagree, or nothing at all if they do not."""
    if got.dtype != expected.dtype:
        return {"reason": f"the replay wrote {got.dtype.str}, the capture holds {expected.dtype.str}"}
    if got.shape != expected.shape:
        return {"reason": f"the replay wrote shape {got.shape}, the capture holds {expected.shape}"}
    if np.array_equal(got, expected):
        return None
    return {
        "reason": "the replay's values are not the captured ones",
        # The size of the disagreement, so a reader can tell a driver that
        # is one rounding apart from one that is answering a different
        # question. It is not a threshold: any difference at all fails.
        "max_abs": float(np.max(np.abs(got.astype("f8") - expected.astype("f8")))),
    }


def _first_difference(cases: dict, outputs: dict) -> dict | None:
    """The first captured output the replay did not reproduce, in case order."""
    for name in sorted(cases):
        written = outputs.get(name, {})
        for variable in sorted(cases[name]["outputs"]):
            if variable not in written:
                return {
                    "case": name, "variable": variable,
                    "reason": "the replay wrote no output for this captured variable",
                }
            found = difference(
                cases[name]["outputs"][variable],
                npy.decode(base64.b64decode(written[variable])),
            )
            if found is not None:
                return {"case": name, "variable": variable, **found}
    return None


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Run every captured case through the replay driver and compare.

    The detail names, per dataset, how many cases were compared, the
    capture set they came from, and -- when they disagree -- the first
    case and variable that did, with how far apart they were. Raises
    ComponentError if the builder could not be reached.
    """
    manifest = ctx.provenance.manifest()
    sets = harness_capture.captured_sets(ctx)
    replay = manifest.build.targets[REPLAY_ROLE]
    attempt_id = ctx.provenance.attempt_id()

    per_dataset = {}
    disagreed = []
    reasons = []
    executable_identity = None
    for name in sorted(sets):
        cases = ctx.sets.load(sets[name])
        resp = backend.replay(ctx.builder, attempt_id, replay.executable, wire_inputs(cases))

        entry = {"cases": len(cases), CAPTURE_SET_KEY: sets[name]}
        executable_identity = executable_identity or resp.executable_identity
        if not resp.ok:
            entry["log_tail"] = resp.log_tail
            disagreed.append(name)
            reasons.append(f"the replay of dataset '{name}' would not run")
        else:
            first = _first_difference(cases, resp.outputs)
            if first is not None:
                entry["first_difference"] = first
                disagreed.append(name)
                reasons.append(
                    f"dataset '{name}', case '{first['case']}', variable "
                    f"'{first['variable']}': {first['reason']}"
                )
        per_dataset[name] = entry

    detail = {
        "manifest_sha256": manifest.sha256,
        EXECUTABLE_IDENTITY_KEY: executable_identity,
        "datasets": per_dataset,
        "datasets_that_disagreed": disagreed,
    }
    materials = capture_set_materials(detail)
    if disagreed:
        return CheckResult(
            verdict=FAIL, detail=detail, reasons=tuple(reasons), materials=materials,
        )
    return CheckResult(verdict=PASS, detail=detail, materials=materials)
