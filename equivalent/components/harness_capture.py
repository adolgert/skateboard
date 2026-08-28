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
from equivalent.gateway.submit import attempt_id_for_strategy
from equivalent.ledger.capture_sets import store_capture_set
from equivalent.ledger.store import LedgerStore
from equivalent.strategy.schema import Strategy

from . import tree_manifest
from .errors import ComponentError

# The manifest role of the program that writes a dataset.
CAPTURE_ROLE = "capture"
# The two datasets a port is judged by, and the pair that must not be the
# same run. A code may declare more; nothing is held back in those.
VISIBLE, HOLDOUT = "visible", "holdout"
# What a case's two halves are called on the wire and in the manifest's
# interface, in the words a message about one should use.
SECTIONS = (("inputs", "input"), ("outputs", "output"))


def captured_sets(store: LedgerStore, tree) -> dict:
    """The capture set each dataset was stored under, from the tree's own claim.

    The checks that follow this one compare against the sets this one
    approved, and they find them the same way regression_visible finds
    the outputs of a run: by reading the claim, not by capturing again.
    A tree with no passing capture claim is an error, not a verdict --
    the gateway's own precondition table is what should have stopped the
    request before it got here.
    """
    claim = store.latest("harness/captured", tree)
    if claim is None or claim.predicate.verdict != "pass":
        raise ComponentError(f"no passing harness/captured claim for tree {tree.sha256}")
    sets = {
        name: entry["capture_set"]
        for name, entry in claim.predicate.detail.get("datasets", {}).items()
        if entry.get("capture_set")
    }
    if not sets:
        raise ComponentError(
            f"the harness/captured claim for tree {tree.sha256} names no capture set"
        )
    return sets


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


def check(store: LedgerStore, repo_dir, ref: str, region_id: str, tree_sha: str,
          baseline_strategy: Strategy, builder) -> dict:
    """Capture every dataset the tree's manifest declares, and store what passes.

    The capture program is run in the baseline strategy's workspace: the
    reference answers a port is judged against are the ones the code
    produces when it is built the way the baseline is built.

    Returns {"verdict": "pass" | "fail", "detail": {...}}, where the detail
    names, per dataset, how many cases were captured and the capture set
    the ledger now holds them under. Raises ComponentError if the builder
    could not be reached.
    """
    manifest = tree_manifest.manifest_of(repo_dir, ref)
    attempt_id = attempt_id_for_strategy(region_id, tree_sha, baseline_strategy.name)
    described = {"manifest_sha256": manifest.sha256}

    capture = manifest.build.targets.get(CAPTURE_ROLE)
    if capture is None:
        # The manifest loader does not insist on a capture target, because
        # a promoted code is never captured again -- so a code being
        # brought in learns it here.
        return {
            "verdict": "fail",
            "detail": {
                **described,
                "problems": [
                    f"code '{manifest.name}' declares no '{CAPTURE_ROLE}' build target, so "
                    f"there is no program to write the datasets it declares"
                ],
            },
        }

    per_dataset = {}
    captured = {}
    problems = []
    for name in sorted(manifest.datasets):
        try:
            resp = builder.capture(
                attempt_id, capture.executable, list(manifest.datasets[name].args), name,
            )
        except Exception as exc:
            raise ComponentError(f"builder /v1/capture call failed: {exc}") from exc

        cases = resp.get("cases", {}) if resp.get("ok") else {}
        per_dataset[name] = {"cases": len(cases)}
        if not cases:
            problems.append(
                f"dataset '{name}': the capture program wrote no case; "
                f"{resp.get('stdout_tail', '')}".strip()
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
        # Nothing is stored: a set that failed its own check must not be
        # in the ledger for a later comparison to find.
        return {"verdict": "fail", "detail": {**described, "datasets": per_dataset, "problems": problems}}

    for name, cases in captured.items():
        subject = store_capture_set(
            store, name, {case: case_arrays(cases[case]) for case in cases},
        )
        per_dataset[name]["capture_set"] = subject.sha256
    return {"verdict": "pass", "detail": {**described, "datasets": per_dataset}}
