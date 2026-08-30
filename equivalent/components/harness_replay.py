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
from equivalent.gateway.submit import attempt_id_for_strategy
from equivalent.ledger.capture_sets import load_capture_set
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy

from . import harness_capture, tree_manifest
from .errors import ComponentError

# The manifest role of the driver that replays one case.
REPLAY_ROLE = "replay"


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


def check(store: LedgerStore, tree: Subject, repo_dir, ref: str, region_id: str, tree_sha: str,
          baseline_strategy: Strategy, builder) -> dict:
    """Run every captured case through the replay driver and compare.

    Returns {"verdict": "pass" | "fail", "detail": {...}}, where the detail
    names, per dataset, how many cases were compared, the capture set they
    came from, and -- when they disagree -- the first case and variable
    that did, with how far apart they were. Raises ComponentError if the
    tree has no passing capture claim or the builder could not be reached.
    """
    manifest = tree_manifest.manifest_of(repo_dir, ref)
    sets = harness_capture.captured_sets(store, tree)
    replay = manifest.build.targets[REPLAY_ROLE]
    attempt_id = attempt_id_for_strategy(region_id, tree_sha, baseline_strategy.name)

    per_dataset = {}
    failed = []
    for name in sorted(sets):
        cases = load_capture_set(store, sets[name])
        try:
            resp = builder.run(
                attempt_id, replay.executable, wire_inputs(cases),
                notify=None, mandatory=False,
            )
        except Exception as exc:
            raise ComponentError(f"builder /v1/run call failed: {exc}") from exc

        entry = {"cases": len(cases), "capture_set": sets[name]}
        if not resp.get("ok"):
            entry["log_tail"] = resp.get("log_tail", "")
            failed.append(name)
        else:
            first = _first_difference(cases, resp.get("outputs", {}))
            if first is not None:
                entry["first_difference"] = first
                failed.append(name)
        per_dataset[name] = entry

    return {
        "verdict": "fail" if failed else "pass",
        "detail": {
            "manifest_sha256": manifest.sha256,
            "datasets": per_dataset,
            "datasets_that_disagreed": failed,
        },
    }
