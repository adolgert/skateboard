"""Runs the code's own capture program and judges the datasets it wrote.

Trust role: what this returns becomes a claim, and the capture sets it
stores are what every later comparison is made against -- the replay
check, the determinism check, and, once the code is promoted, the
oracle's answers about a port. If it approved a case that does not hold
what the region's interface declares, or a held-out set that is the
visible set under another name, every claim above it would be measuring
something other than what the person reading it believes.

Three things are asked of each run. The capture program must write at
least one case for every dataset the manifest declares. Every case must
hold exactly the variables the region declares, going in and coming out,
each of the declared element type and rank. And the visible and held-out
datasets must be different runs: two parameter sets that produce the
same inputs hold nothing back, and a port would be judged twice against
data the agent had already seen.

All three are verdicts about the agent's own work -- the capture program
and the manifest are what it wrote -- so a failure is a `fail` naming the
dataset, the case, and the variable, not an error. An error here means
the builder could not be reached at all.
"""
from __future__ import annotations

import base64
import hashlib

from equivalent.capture import npy
from equivalent.ledger.capture_sets import (
    CAPTURED_PREDICATE,
    pack_capture_set,
    sets_named_by,
)

from equivalent.ledger.vocabulary import CAPTURE_SET_KEY, EXECUTABLE_IDENTITY_KEY, PASS
from . import backend
from .context import CheckContext, CheckResult, capture_set_materials, failed
from .errors import ComponentError
from .names import CAPTURE_ROLE, HOLDOUT, VISIBLE

# What a manifest with no capture target leaves the harness without, in
# the words the message about it uses.
NO_PROGRAM_TO_CAPTURE = "no program to write the datasets it declares"
# What a case's two halves are called on the wire and in the manifest's
# interface, in the words a message about one should use.
SECTIONS = (("inputs", "input"), ("outputs", "output"))


def captured_sets(ctx: CheckContext) -> dict:
    """The capture set each dataset was stored under, from the tree's own claim.

    The checks that follow this one compare against the sets this one
    approved, and they find them the same way regression_visible finds
    the outputs of a run: by reading the claim, not by capturing again.
    The claim itself is always there -- the precondition table is what
    puts it in the context.

    A claim naming no set is broken evidence. To a check that is a fault
    on the harness's side rather than a verdict about the code, so it
    comes back as an error rather than as a fail.
    """
    try:
        return sets_named_by(ctx.claims[CAPTURED_PREDICATE], f"tree {ctx.tree.sha}")
    except ValueError as exc:
        raise ComponentError(str(exc)) from exc


def _declared(manifest, section: str) -> dict:
    """The region's variables of one half of a case, by name."""
    variables = manifest.interface.inputs if section == "inputs" else manifest.interface.outputs
    return {variable.name: variable for variable in variables}


def _case_problems(manifest, where: str, case: dict) -> list:
    """Everything wrong with one captured case, one line each."""
    problems = []
    for section, word in SECTIONS:
        declared = _declared(manifest, section)
        captured = case.get(section, {})
        for name in sorted(set(declared) - set(captured)):
            problems.append(
                f"{where}: no {word} '{name}' was captured, which code "
                f"'{manifest.name}' declares the region has"
            )
        for name in sorted(set(captured) - set(declared)):
            problems.append(
                f"{where}: {word} '{name}' was captured, which code "
                f"'{manifest.name}' does not declare; it declares {sorted(declared)}"
            )
        for name in sorted(set(captured) & set(declared)):
            try:
                npy.check(npy.decode(base64.b64decode(captured[name])), declared[name])
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
    return problems


def case_arrays(case: dict) -> dict:
    """One case as arrays, the shape a capture set is stored in."""
    return {
        section: {
            name: npy.decode(base64.b64decode(data))
            for name, data in case.get(section, {}).items()
        }
        for section, _ in SECTIONS
    }


def _input_fingerprint(cases: dict) -> list:
    """What the inputs of a whole dataset are, ignoring what the cases are called.

    Two runs of the capture program with different parameters may still
    name their cases the same way, so the names are left out: what says
    two datasets are the same run is the arrays.
    """
    return sorted(
        sorted(
            (name, hashlib.sha256(base64.b64decode(data)).hexdigest())
            for name, data in case.get("inputs", {}).items()
        )
        for case in cases.values()
    )


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Capture every dataset the tree's manifest declares, and pack what passes.

    The capture program is run in the baseline strategy's workspace: the
    reference answers a port is judged against are the ones the code
    produces when it is built the way the baseline is built.

    The detail names, per dataset, how many cases were captured and the
    capture set the ledger will hold them under; the sets themselves come
    back for the gateway to keep, so a failing capture leaves nothing
    behind. Raises ComponentError if the builder could not be reached.
    """
    manifest = ctx.provenance.manifest()
    attempt_id = ctx.provenance.attempt_id()
    described = {"manifest_sha256": manifest.sha256}

    # The manifest loader does not insist on a capture target, because a
    # promoted code is never captured again -- so a code being brought in
    # learns it here, as a verdict about the manifest it wrote.
    capture, refusal = ctx.provenance.build_target(
        manifest, CAPTURE_ROLE, NO_PROGRAM_TO_CAPTURE, described,
    )
    if refusal is not None:
        return refusal

    per_dataset = {}
    captured = {}
    executable_identity = None
    problems = []
    for name in sorted(manifest.datasets):
        resp = backend.capture(
            ctx.builder, attempt_id, capture.executable, manifest.datasets[name].args, name,
        )

        cases = resp.cases if resp.ok else {}
        executable_identity = executable_identity or resp.executable_identity
        per_dataset[name] = {"cases": len(cases)}
        if not cases:
            problems.append(
                f"dataset '{name}': the capture program wrote no case; "
                f"{resp.stdout_tail}".strip()
            )
            continue
        captured[name] = cases
        for case_name in sorted(cases):
            problems.extend(
                _case_problems(manifest, f"dataset '{name}', case '{case_name}'", cases[case_name])
            )

    if VISIBLE in captured and HOLDOUT in captured:
        if _input_fingerprint(captured[VISIBLE]) == _input_fingerprint(captured[HOLDOUT]):
            problems.append(
                f"datasets '{VISIBLE}' and '{HOLDOUT}' were captured with different "
                f"parameters and produced the same inputs, so the held-out set holds "
                f"nothing back from a port"
            )

    if problems:
        # Nothing is packed for keeping: a set that failed its own check
        # must not be in the ledger for a later comparison to find.
        return failed(
            {
                **described, "datasets": per_dataset, "problems": problems,
                EXECUTABLE_IDENTITY_KEY: executable_identity,
            },
            problems,
        )

    packed = []
    for name, cases in captured.items():
        one = pack_capture_set(name, {case: case_arrays(cases[case]) for case in cases})
        per_dataset[name][CAPTURE_SET_KEY] = one.sha256
        packed.append(one)
    detail = {
        **described, "datasets": per_dataset,
        EXECUTABLE_IDENTITY_KEY: executable_identity,
    }
    return CheckResult(
        verdict=PASS, detail=detail,
        materials=capture_set_materials(detail), stores=tuple(packed),
    )
