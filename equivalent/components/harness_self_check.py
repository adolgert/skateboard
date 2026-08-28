"""Asks the harness whether it would notice a wrong port of this region.

Trust role: what this returns becomes a claim, and it is the only claim
about the gate rather than about the code. Every other onboarding check
says the harness ran; this one says the harness can tell right from
wrong. Single-token faults are injected into the files the manifest says
implement the region, each is built and replayed the way the baseline is,
and each answer is compared with the captured one by the comparator and
the bands a port will be judged by. A wrong verdict here would certify a
gate that cannot fail a bad port, and every later claim about that code
would rest on it.

Three things fail the check, and each says something different:

  * No mutant was generated. There is nothing to say about a gate nobody
    could put a fault past.
  * No mutant was killed. Every fault injected into the region went
    unnoticed, so the harness is not a harness -- usually a replay driver
    that does not write what it computed, or captured inputs that never
    reach the region.
  * Some mutant landed in the tolerance-blind gap: its answer differed
    from the captured one and every band let it through. That is a wrong
    kernel this policy would accept, and it is the number to hold at
    zero. The bands are the thing to change, not this check.

A survivor -- a mutant no output changed at all for -- is not a failure.
It is either code that really is equivalent or region the captured
inputs never reach, and nothing here can tell those apart. They are
listed in the detail for the person, which is the whole point of running
this while a person is still reading.
"""
from __future__ import annotations

import base64
import hashlib
import json

from equivalent.capture import npy
from equivalent.gateway.submit import attempt_id_for_strategy
from equivalent.ledger.capture_sets import load_capture_set
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy

from . import build_replay, harness_capture, tree_manifest
from .errors import ComponentError

# The manifest role of the driver each mutant is replayed through.
REPLAY_ROLE = "replay"
# The dataset the mutants are scored against: the one the agent can see,
# because a self-check is about the harness and holds nothing back.
VISIBLE = "visible"
# Where the tolerance policy keeps a band per region output variable.
# The `files` section beside it bands a whole-program run, which is a
# different measurement and would answer a different question.
VARIABLE_BANDS = "variables"

# What the builder calls a mutant it noticed, one it could not, and one
# whose answer changed inside every band.
KILLED = "KILLED"
EQUIVALENT = "EQUIVALENT"
GAP = "GAP"
# What a named mutant is reported as, so the person can open the file at
# that line and read the change.
NAMED_FIELDS = ("id", "file", "line", "op", "mutated", "note")


def wire_cases(cases: dict) -> dict:
    """A stored capture set as the builder's mutation stage wants it.

    Both halves travel: the inputs are what each mutant is replayed on,
    and the outputs are what its answers are compared with. Nothing else
    holds the captured answers, so a mutation run cannot be scored
    without them.
    """
    return {
        name: {
            section: {
                variable: base64.b64encode(npy.encode(array)).decode()
                for variable, array in case.get(section, {}).items()
            }
            for section in ("inputs", "outputs")
        }
        for name, case in cases.items()
    }


def bands_of(policy_bytes: bytes) -> dict:
    """The band per output variable, from the tolerance file the tree carries."""
    try:
        bands = json.loads(policy_bytes)[VARIABLE_BANDS]
        if not isinstance(bands, dict):
            raise TypeError(f"'{VARIABLE_BANDS}' is not a mapping")
    except (ValueError, KeyError, TypeError) as exc:
        raise ComponentError(
            f"the tree's tolerance policy names no band per output variable under "
            f"'{VARIABLE_BANDS}', although a passing manifest claim says it does: {exc}"
        ) from exc
    return bands


def _named(rows, status: str) -> list:
    """The mutants of one verdict, in the words a person reads them in."""
    return [
        {field: row.get(field) for field in NAMED_FIELDS}
        for row in rows if row.get("status") == status
    ]


def _problems(generated: int, counts: dict, gap: list) -> list:
    problems = []
    if not generated:
        problems.append(
            "no mutant could be made of the files the manifest says implement the "
            "region, so nothing was asked of the harness"
        )
    elif not counts.get(KILLED):
        problems.append(
            "no mutant was killed: every fault injected into the region went unnoticed "
            "by this harness, so it would not notice a wrong port either"
        )
    if gap:
        problems.append(
            f"{len(gap)} mutant(s) changed an output and stayed inside the tolerance "
            f"bands; each is a wrong kernel this policy would accept, so the bands are "
            f"what has to change"
        )
    return problems


def check(store: LedgerStore, tree: Subject, repo_dir, ref: str, region_id: str, tree_sha: str,
          baseline_strategy: Strategy, builder, *, limit=None) -> dict:
    """Mutate the region's files, score every mutant, and judge the harness.

    Returns {"verdict": "pass" | "fail", "detail": {...}}: how many
    mutants were made and scored, how many landed in each verdict, every
    mutant in the tolerance-blind gap, the survivors, and the two things
    the verdict rests on -- the visible capture set and the tolerance
    policy -- which the caller files as the claim's materials. Raises
    ComponentError if the tree has no passing capture claim, if the
    policy cannot be read, or if the builder could not run the mutation
    at all.
    """
    manifest, policy_bytes = tree_manifest.manifest_and_policy(repo_dir, ref)
    sets = harness_capture.captured_sets(store, tree)
    if VISIBLE not in sets:
        raise ComponentError(
            f"the capture claim for tree {tree.sha256} names no '{VISIBLE}' dataset, so "
            f"there are no answers to score a mutant against"
        )
    cases = load_capture_set(store, sets[VISIBLE])
    bands = bands_of(policy_bytes)
    fortran = build_replay.fortran_of(baseline_strategy)
    replay = manifest.build.targets[REPLAY_ROLE]

    try:
        resp = builder.mutate(
            attempt_id_for_strategy(region_id, tree_sha, baseline_strategy.name),
            manifest.build.makefile,
            {"target": replay.target, "executable": replay.executable},
            list(manifest.interface.files),
            wire_cases(cases),
            bands,
            fortran.compiler,
            list(fortran.flags),
            list(baseline_strategy.link_flags),
            list(manifest.source.patterns),
            limit=None if limit is None else int(limit),
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/mutate call failed: {exc}") from exc
    if not resp.get("ok"):
        # The builder refused to run at all -- an unbuilt tree, a file
        # that is not in it. That is the harness's own footing, not a
        # verdict about whether this gate can tell right from wrong.
        raise ComponentError(f"the mutation run did not start: {resp.get('log_tail', '')}")

    rows = resp.get("results", [])
    counts = resp.get("counts", {})
    gap = _named(rows, GAP)
    problems = _problems(resp.get("generated", 0), counts, gap)
    detail = {
        "manifest_sha256": manifest.sha256,
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "files": list(manifest.interface.files),
        "datasets": {VISIBLE: {"cases": len(cases), "capture_set": sets[VISIBLE]}},
        "generated": resp.get("generated", 0),
        "scored": resp.get("scored", 0),
        "counts": counts,
        "gap": gap,
        # Not a failure, and the reason the person is reading this claim:
        # each one is either equivalent code or region the captured
        # inputs never reach, and only a reader can say which.
        "survivors": _named(rows, EQUIVALENT),
        "kept_dirs": resp.get("kept_dirs", []),
    }
    if problems:
        return {"verdict": "fail", "detail": {**detail, "problems": problems}}
    return {"verdict": "pass", "detail": detail}
