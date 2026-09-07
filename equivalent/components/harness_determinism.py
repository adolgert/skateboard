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
from equivalent.ledger.capture_sets import pack_capture_set

from . import backend, harness_capture, harness_replay
from .context import CheckContext, CheckResult, capture_set_materials
from .errors import ComponentError

# What the second run of a dataset's capture is called, so it writes into
# a directory of its own: a program that appends to what is already there
# would otherwise look deterministic.
AGAIN = "-again"


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


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Capture every dataset again and replay the visible inputs twice.

    The detail says, per dataset, the set that was stored and the set the
    second capture produced; separately, whether the two replay runs
    agreed and where they first did not; and `differed`, which names in
    words whichever of the repeats disagreed. Raises ComponentError if
    the builder could not be reached.
    """
    manifest = ctx.provenance.manifest()
    sets = harness_capture.captured_sets(ctx)
    capture, refusal = ctx.provenance.build_target(
        manifest, harness_capture.CAPTURE_ROLE, harness_capture.NO_PROGRAM_TO_CAPTURE,
        {"manifest_sha256": manifest.sha256},
    )
    if refusal is not None:
        return refusal
    attempt_id = ctx.provenance.attempt_id()

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
        resp = backend.capture(
            ctx, attempt_id, capture.executable, dataset.args, f"{name}{AGAIN}",
        )
        cases = resp.cases if resp.ok else {}
        if not cases:
            entry["same"] = False
            entry["stdout_tail"] = resp.stdout_tail
            differed.append(f"the second capture of dataset '{name}' wrote no case")
        else:
            again = pack_capture_set(
                name, {case: harness_capture.case_arrays(cases[case]) for case in cases},
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
    visible = ctx.sets.load(sets[harness_capture.VISIBLE])
    replay = manifest.build.targets[harness_replay.REPLAY_ROLE]
    replay_detail = {"dataset": harness_capture.VISIBLE, "cases": len(visible)}
    cases = harness_replay.wire_inputs(visible)
    runs = [
        backend.replay(ctx, attempt_id, replay.executable, cases),
        backend.replay(ctx, attempt_id, replay.executable, cases),
    ]
    replay_detail["executable_identity"] = next(
        (run.executable_identity for run in runs if run.executable_identity), None,
    )
    if not all(run.ok for run in runs):
        failing = next(run for run in runs if not run.ok)
        replay_detail["same"] = False
        replay_detail["log_tail"] = failing.log_tail
        differed.append("a repeat of the replay would not run")
    else:
        first = _repeat_difference(runs[0].outputs, runs[1].outputs)
        replay_detail["same"] = first is None
        if first is not None:
            replay_detail["first_difference"] = first
            differed.append(
                f"the two replay runs disagree on case '{first['case']}', variable "
                f"'{first['variable']}'"
            )

    detail = {
        "manifest_sha256": manifest.sha256,
        "executable_identity": replay_detail.get("executable_identity"),
        "datasets": per_dataset,
        "replay": replay_detail,
        "differed": differed,
    }
    return CheckResult(
        verdict="fail" if differed else "pass",
        detail=detail,
        reasons=tuple(differed),
        materials=capture_set_materials(detail),
    )
