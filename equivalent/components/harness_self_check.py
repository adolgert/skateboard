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
from equivalent.ledger.subjects import Subject

from . import build_replay, harness_capture
from .context import CheckContext, CheckResult, capture_set_materials
from .errors import ComponentError, after_the_manifest_check_passed
# The mutants are scored on the dataset the agent can see, within the
# bands a port's own region outputs are judged by: a self-check is about
# the harness and holds nothing back.
from .names import REPLAY_ROLE, VARIABLE_BANDS, VISIBLE

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


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Mutate the region's files, score every mutant, and judge the harness.

    `limit` scores only the first mutants. It is for a session finding its
    feet on a large region; the claim says how many there were, so a
    limited run cannot be mistaken for a whole one.

    The detail says how many mutants were made and scored, how many landed
    in each verdict, every mutant in the tolerance-blind gap, the
    survivors, and the two things the verdict rests on -- the visible
    capture set and the tolerance policy -- which come back as the claim's
    materials. Raises ComponentError if the policy cannot be read, or if
    the builder could not run the mutation at all.
    """
    limit = config.get("limit")
    with after_the_manifest_check_passed():
        manifest, policy_bytes = ctx.tree.manifest_and_policy()
    sets = harness_capture.captured_sets(ctx)
    if VISIBLE not in sets:
        raise ComponentError(
            f"the capture claim for tree {ctx.tree.sha} names no '{VISIBLE}' dataset, so "
            f"there are no answers to score a mutant against"
        )
    cases = ctx.sets.load(sets[VISIBLE])
    bands = bands_of(policy_bytes)
    fortran = build_replay.fortran_of(ctx.baseline_strategy)
    replay = manifest.build.targets[REPLAY_ROLE]

    try:
        resp = ctx.builder.mutate(
            ctx.provenance.attempt_id(),
            manifest.build.makefile,
            {"target": replay.target, "executable": replay.executable},
            list(manifest.interface.files),
            wire_cases(cases),
            bands,
            fortran.compiler,
            list(fortran.flags),
            list(ctx.baseline_strategy.link_flags),
            list(manifest.source.patterns),
            limit=None if limit is None else int(limit),
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/mutate call failed: {exc}") from exc
    if not resp.ok:
        # The builder refused to run at all -- an unbuilt tree, a file
        # that is not in it. That is the harness's own footing, not a
        # verdict about whether this gate can tell right from wrong.
        raise ComponentError(f"the mutation run did not start: {resp.log_tail}")

    rows = resp.results
    counts = resp.counts
    response_problems = []
    derived_counts = {}
    for row in rows:
        status = row.get("status")
        derived_counts[status] = derived_counts.get(status, 0) + 1
    if resp.scored != len(rows) or counts != derived_counts:
        response_problems.append(
            "builder returned mutation counts inconsistent with its result rows"
        )
    ids = [row.get("id") for row in rows]
    if any(not isinstance(mid, str) or not mid for mid in ids) or len(ids) != len(set(ids)):
        response_problems.append("builder returned missing or duplicate mutant identifiers")

    gap = _named(rows, GAP)
    problems = [*response_problems, *_problems(resp.generated, rows, counts, gap)]
    incomplete = [
        {field: row.get(field) for field in NAMED_FIELDS}
        for row in rows if row.get("status") in INCOMPLETE_STATUSES
    ]
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    detail = {
        "manifest_sha256": manifest.sha256,
        "policy_sha256": policy_sha256,
        "files": list(manifest.interface.files),
        "datasets": {VISIBLE: {"cases": len(cases), "capture_set": sets[VISIBLE]}},
        "generated": resp.generated,
        "scored": resp.scored,
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
        "kept_dirs": resp.kept_dirs,
    }
    # The two things this verdict rests on: the answers the mutants were
    # scored against, and the bands that decided whether a changed answer
    # counted.
    materials = (
        *capture_set_materials(detail),
        Subject(kind="policy", sha256=policy_sha256),
    )
    if problems:
        return CheckResult(
            verdict="fail", detail={**detail, "problems": problems},
            reasons=tuple(problems), materials=materials,
        )
    return CheckResult(verdict="pass", detail=detail, materials=materials)
