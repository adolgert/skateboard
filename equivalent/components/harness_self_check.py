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
  * Some selected mutation changed an answer but remained inside every
    band. For this deliberately conservative self-check policy, that needs
    human review of the mutation, workload, and numerical policy.

A row whose output did not change is not called equivalent: it may be
equivalent, or the captured inputs may never reach it. Such rows are listed
for review. Build failures are also listed but do not count as evidence that
the harness detects a numerical fault. Runtime failures, skipped rows, and
pending rows make the run incomplete and prevent a passing adequacy claim.
"""
from __future__ import annotations

import base64
import hashlib
import json

from equivalent.capture import npy
from equivalent.ledger.capture_sets import load_capture_set
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy
from equivalent.tree import Tree, attempt_id_for_strategy

from . import build_replay, harness_capture
from .errors import ComponentError, after_the_manifest_check_passed

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
BUILD_FAIL = "BUILD_FAIL"
RUNTIME_FAIL = "RUNTIME_FAIL"
SKIPPED = "SKIPPED"
PENDING = "PENDING"
KNOWN_STATUSES = frozenset({KILLED, EQUIVALENT, GAP, BUILD_FAIL, RUNTIME_FAIL, SKIPPED, PENDING})
INCOMPLETE_STATUSES = frozenset({RUNTIME_FAIL, SKIPPED, PENDING})
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


def _problems(generated: int, rows: list, counts: dict, gap: list) -> list:
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
            f"bands; the mutation, workload, and numerical policy require review"
        )
    if generated != len(rows):
        problems.append(
            f"not all generated mutants were classified: generated {generated}, "
            f"received {len(rows)} result row(s)"
        )
    incomplete = [row for row in rows if row.get("status") in INCOMPLETE_STATUSES]
    if incomplete:
        problems.append(
            f"{len(incomplete)} mutant(s) did not complete numerical scoring"
        )
    unknown = [row for row in rows if row.get("status") not in KNOWN_STATUSES]
    if unknown:
        problems.append(f"{len(unknown)} mutant(s) have an unknown status")
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
    with after_the_manifest_check_passed():
        manifest, policy_bytes = Tree(repo_dir, ref).manifest_and_policy()
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

    rows = resp.get("results")
    counts = resp.get("counts")
    generated = resp.get("generated")
    scored = resp.get("scored")
    response_problems = []
    if isinstance(generated, bool) or not isinstance(generated, int) or generated < 0:
        response_problems.append("builder returned an invalid generated count")
        generated = 0
    if isinstance(scored, bool) or not isinstance(scored, int) or scored < 0:
        response_problems.append("builder returned an invalid scored count")
        scored = 0
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        response_problems.append("builder returned no valid mutation result list")
        rows = []
    if not isinstance(counts, dict):
        response_problems.append("builder returned no valid mutation status counts")
        counts = {}

    derived_counts = {}
    for row in rows:
        status = row.get("status")
        derived_counts[status] = derived_counts.get(status, 0) + 1
    if scored != len(rows) or counts != derived_counts:
        response_problems.append(
            "builder returned mutation counts inconsistent with its result rows"
        )
    ids = [row.get("id") for row in rows]
    if any(not isinstance(mid, str) or not mid for mid in ids) or len(ids) != len(set(ids)):
        response_problems.append("builder returned missing or duplicate mutant identifiers")

    gap = _named(rows, GAP)
    problems = [*response_problems, *_problems(generated, rows, counts, gap)]
    incomplete = [
        {field: row.get(field) for field in NAMED_FIELDS}
        for row in rows if row.get("status") in INCOMPLETE_STATUSES
    ]
    detail = {
        "manifest_sha256": manifest.sha256,
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "files": list(manifest.interface.files),
        "datasets": {VISIBLE: {"cases": len(cases), "capture_set": sets[VISIBLE]}},
        "generated": generated,
        "scored": scored,
        "counts": counts,
        "gap": gap,
        "adequacy_policy": {
            "all_generated_classified": True,
            "incomplete_statuses_forbidden": sorted(INCOMPLETE_STATUSES),
            "minimum_killed": 1,
            "maximum_tolerance_gap": 0,
        },
        "incomplete": incomplete,
        # The builder's historical status is EQUIVALENT, but this claim
        # deliberately labels these only as unchanged outputs.
        "survivors": _named(rows, EQUIVALENT),
        "build_failures": _named(rows, BUILD_FAIL),
        "kept_dirs": resp.get("kept_dirs", []),
    }
    if problems:
        return {"verdict": "fail", "detail": {**detail, "problems": problems}}
    return {"verdict": "pass", "detail": detail}
