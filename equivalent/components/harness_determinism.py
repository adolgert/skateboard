"""Captures and replays a second time, and requires the same answers.

Trust role: what this returns becomes a claim, and it is the claim that
says every other harness claim is about the code rather than about one
particular afternoon. A capture program seeded from the clock, a driver
that reads uninitialized memory, a build that reorders a reduction from
run to run -- each of them can pass the capture and replay checks once
and then quietly disagree with the stored answers forever after, and
every regression verdict a port earns against those answers would be
noise.

Two repeats are asked for. The capture program is run again, with the
same arguments and into an output directory of its own, and what it
writes must hash to the capture set already stored -- which is the whole
point of naming a set by its content: the same bytes are the same
subject, and nothing has to be compared array by array to know it. The
replay driver is then run twice on the visible inputs and the two
answers must be identical, bitwise. The second run's answer is compared
with the first run's here rather than with what the replay check
recorded, so that the two runs being compared are two runs of the same
moment.
"""
from __future__ import annotations

import base64

from equivalent.capture import npy
from equivalent.gateway.submit import attempt_id_for_strategy
from equivalent.ledger.capture_sets import load_capture_set, store_capture_set
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy

from . import harness_capture, harness_replay, tree_manifest
from .errors import ComponentError

# What the second run of a dataset's capture is called, so it writes into
# a directory of its own: a program that appends to what is already there
# would otherwise look deterministic.
AGAIN = "-again"


def _recapture(builder, attempt_id: str, executable: str, dataset, name: str) -> dict:
    try:
        return builder.capture(attempt_id, executable, list(dataset.args), f"{name}{AGAIN}")
    except Exception as exc:
        raise ComponentError(f"builder /v1/capture call failed: {exc}") from exc


def _replay(builder, attempt_id: str, executable: str, cases: dict) -> dict:
    try:
        return builder.run(
            attempt_id, executable, harness_replay.wire_inputs(cases),
            notify=None, mandatory=False,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/run call failed: {exc}") from exc


def _repeat_difference(first: dict, second: dict) -> dict | None:
    """The first output the two replay runs did not agree on, in case order."""
    for name in sorted(first):
        written = second.get(name, {})
        for variable in sorted(first[name]):
            if variable not in written:
                return {
                    "case": name, "variable": variable,
                    "reason": "the second run wrote no output for a variable the first wrote",
                }
            if written[variable] == first[name][variable]:
                continue
            found = harness_replay.difference(
                npy.decode(base64.b64decode(first[name][variable])),
                npy.decode(base64.b64decode(written[variable])),
            )
            return {
                "case": name, "variable": variable,
                **(found or {"reason": "the two runs wrote different files for equal arrays"}),
            }
    return None


def check(store: LedgerStore, tree: Subject, repo_dir, ref: str, region_id: str, tree_sha: str,
          baseline_strategy: Strategy, builder) -> dict:
    """Capture every dataset again and replay the visible inputs twice.

    Returns {"verdict": "pass" | "fail", "detail": {...}}. The detail says,
    per dataset, the set that was stored and the set the second capture
    produced; separately, whether the two replay runs agreed and where
    they first did not; and `differed`, which names in words whichever of
    the repeats disagreed. Raises ComponentError if the tree has no
    passing capture claim or the builder could not be reached.
    """
    manifest = tree_manifest.manifest_of(repo_dir, ref)
    sets = harness_capture.captured_sets(store, tree)
    capture = manifest.build.targets.get(harness_capture.CAPTURE_ROLE)
    if capture is None:
        raise ComponentError(
            f"code '{manifest.name}' declares no '{harness_capture.CAPTURE_ROLE}' build "
            f"target, although a passing capture claim for this tree says it did"
        )
    attempt_id = attempt_id_for_strategy(region_id, tree_sha, baseline_strategy.name)

    differed = []
    per_dataset = {}
    for name in sorted(sets):
        entry = {"capture_set": sets[name]}
        dataset = manifest.datasets.get(name)
        if dataset is None:
            # The manifest lost a dataset the capture claim was filed
            # about; that is a changed manifest, not a drifting program.
            raise ComponentError(
                f"the tree's manifest no longer declares dataset '{name}', which the "
                f"capture claim for this tree was filed about"
            )
        resp = _recapture(builder, attempt_id, capture.executable, dataset, name)
        cases = resp.get("cases", {}) if resp.get("ok") else {}
        if not cases:
            entry["same"] = False
            entry["stdout_tail"] = resp.get("stdout_tail", "")
            differed.append(f"the second capture of dataset '{name}' wrote no case")
        else:
            again = store_capture_set(
                store, name, {case: harness_capture.case_arrays(cases[case]) for case in cases},
            )
            entry["recaptured"] = again.sha256
            entry["same"] = again.sha256 == sets[name]
            if not entry["same"]:
                differed.append(
                    f"the second capture of dataset '{name}' is a different set "
                    f"({again.sha256[:12]}) from the one stored ({sets[name][:12]})"
                )
        per_dataset[name] = entry

    if harness_capture.VISIBLE not in sets:
        raise ComponentError(
            f"the capture claim for this tree names no '{harness_capture.VISIBLE}' set to "
            f"replay twice"
        )
    visible = load_capture_set(store, sets[harness_capture.VISIBLE])
    replay = manifest.build.targets[harness_replay.REPLAY_ROLE]
    replay_detail = {"dataset": harness_capture.VISIBLE, "cases": len(visible)}
    runs = [
        _replay(builder, attempt_id, replay.executable, visible),
        _replay(builder, attempt_id, replay.executable, visible),
    ]
    if not all(run.get("ok") for run in runs):
        failing = next(run for run in runs if not run.get("ok"))
        replay_detail["same"] = False
        replay_detail["log_tail"] = failing.get("log_tail", "")
        differed.append("a repeat of the replay would not run")
    else:
        first = _repeat_difference(runs[0].get("outputs", {}), runs[1].get("outputs", {}))
        replay_detail["same"] = first is None
        if first is not None:
            replay_detail["first_difference"] = first
            differed.append(
                f"the two replay runs disagree on case '{first['case']}', variable "
                f"'{first['variable']}'"
            )

    return {
        "verdict": "fail" if differed else "pass",
        "detail": {
            "manifest_sha256": manifest.sha256,
            "datasets": per_dataset,
            "replay": replay_detail,
            "differed": differed,
        },
    }
