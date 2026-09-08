"""Compare the onboarded CPU program with an independently preserved original.

The reviewer supplies the reference outside the submitted tree. Captures,
replay drivers and tolerances written during onboarding cannot alter this
contract. This establishes agreement on named runs, not scientific validity
or completeness of those runs.
"""
from __future__ import annotations

import base64

from equivalent.capture import npy
from equivalent.capture.compare import compare_variable
from equivalent.ledger.artifacts import binary_artifacts
from equivalent.ledger.packed import PackedArtifact
from equivalent.ledger.subjects import Subject, hash_bytes
from equivalent.ledger.vocabulary import (
    EXECUTABLE_IDENTITY_KEY,
    FAIL,
    PASS,
    REFERENCE_KEY,
)
from equivalent.reference.schema import load_reference

from . import backend
from .building import Recipe, build_verdict
from .context import CheckContext
from .result import CheckResult, failed
from .errors import ComponentError, after_the_manifest_check_passed
from .names import TIMING_ROLE
from .workspaces import attempt_id_for_tree

# How many times each program is run. Two: one to compare with the other
# program, and a second to say the first was not a coincidence.
TIMED_RUNS = 2
# What the two workspaces are for, in the names the builder holds them
# under: the reviewed original is built in one of its own, never in the
# workspace the submitted tree is built in.
ORIGINAL = "original"


def _comparison(expected: bytes, actual: bytes, spec: dict) -> dict:
    if spec["comparison"] == "bytes":
        return {PASS: expected == actual}
    try:
        ref, got = npy.decode(expected), npy.decode(actual)
        if ref.dtype.kind not in "fibu" or got.dtype.kind not in "fibu":
            return {PASS: False, "error": "reference arrays must have numeric or logical dtype"}
        tolerance = spec.get("tolerance", {"abs": 0, "rel": 0, "ulp": 0})
        return compare_variable(ref, got, tolerance)
    except Exception as exc:
        return {PASS: False, "error": f"cannot compare reference arrays: {exc}"}


def _artifact(kept: list, encoded) -> tuple:
    """One program output, named by its bytes and set aside to be kept.

    Both programs' outputs are filed beside the claim, because the claim
    says two runs agreed and a person reading it later has to be able to
    look at what they agreed on. Nothing is written here: what is kept is
    handed back with the verdict, so a comparison that could not be made
    leaves no artifacts claiming it was.
    """
    data = base64.b64decode(encoded, validate=True)
    sha = hash_bytes(data)
    kept.append(PackedArtifact(sha256=sha, data=data))
    return data, sha


def _one_run(builder, run: dict, original, candidate_executable: str,
             reference_attempt: str, candidate_attempt: str, kept: list,
             measured: list) -> tuple:
    """One named run of both programs, compared output by output.

    Answers `(report, problem)`. The problem is not about one output but
    about the run: two programs that did not both finish twice have not
    been compared at all, and the outputs of such a run say nothing.
    """
    original_outputs = sorted({o["original"] for o in run["outputs"]})
    candidate_outputs = sorted({o["candidate"] for o in run["outputs"]})
    try:
        expected = backend.time(builder, reference_attempt, original.executable,
                                run["original_args"], run["env"], original_outputs,
                                TIMED_RUNS, run["budget_s"])
        actual = backend.time(builder, candidate_attempt, candidate_executable,
                              run["candidate_args"], run["env"], candidate_outputs,
                              TIMED_RUNS, run["budget_s"])
    except Exception as exc:
        raise ComponentError(
            f"original comparison run {run['name']!r} could not finish: {exc}"
        ) from exc

    measured.extend(binary_artifacts(
        expected.executable_identity, executable=original.executable,
    ))
    measured.extend(binary_artifacts(
        actual.executable_identity, executable=candidate_executable,
    ))

    report = {"name": run["name"], "outputs": [], PASS: False,
              "original_runs_s": expected.runs_s,
              "candidate_runs_s": actual.runs_s,
              "original_binary": {EXECUTABLE_IDENTITY_KEY: expected.executable_identity},
              "candidate_binary": {EXECUTABLE_IDENTITY_KEY: actual.executable_identity}}
    if any(not r.ok or len(r.outputs) != TIMED_RUNS or len(r.runs_s) != TIMED_RUNS
           for r in (expected, actual)):
        return report, f"{run['name']}: both programs must complete two runs"

    for output in run["outputs"]:
        result = {"original": output["original"], "candidate": output["candidate"],
                  "comparison": output["comparison"], PASS: False}
        report["outputs"].append(result)
        try:
            reference_files = [_artifact(kept, r[output["original"]]) for r in expected.outputs]
            candidate_files = [_artifact(kept, r[output["candidate"]]) for r in actual.outputs]
            result["original_artifacts"] = [f[1] for f in reference_files]
            result["candidate_artifacts"] = [f[1] for f in candidate_files]
            result["deterministic"] = (reference_files[0][0] == reference_files[1][0]
                                       and candidate_files[0][0] == candidate_files[1][0])
            result["comparison_result"] = _comparison(
                reference_files[0][0], candidate_files[0][0], output,
            )
            result[PASS] = result["deterministic"] and result["comparison_result"][PASS]
        except (KeyError, ValueError, TypeError) as exc:
            result["error"] = f"missing or malformed program output: {exc}"
    report[PASS] = all(o[PASS] for o in report["outputs"])
    return report, None


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Build the reviewed original, run both programs, and require agreement.

    The original is built in a workspace of its own, under the baseline
    strategy, and judged by the same three statements every other build
    is: the reference's own recipe is not a manifest's, but what makes a
    build count is the same either way.

    Both programs' outputs are kept beside a claim that says they agreed,
    so a person can look at what they agreed on. Only such a claim keeps
    them: a comparison that did not come out agreeing is not evidence
    about either program, and filing its files beside it would put bytes
    in the ledger that no claim stands behind.
    """
    if ctx.original_reference_path is None:
        problems = [
            "No reviewed original_reference is configured. Preserve the original program "
            "and comparison contract outside the agent working copy before onboarding."
        ]
        return failed({"problems": problems}, problems)
    try:
        original = load_reference(ctx.original_reference_path)
    except (ValueError, OSError) as exc:
        raise ComponentError(f"cannot read reviewed original reference: {exc}") from exc
    with after_the_manifest_check_passed():
        manifest = ctx.tree.manifest()
    candidate = manifest.build.targets.get(TIMING_ROLE)
    if candidate is None:
        problems = ["candidate declares no timing target"]
        return failed({"problems": problems}, problems)

    baseline_strategy = ctx.baseline_strategy
    reference_attempt = attempt_id_for_tree(
        ctx.region_id, ORIGINAL, original.sha256, baseline_strategy.name,
    )
    candidate_attempt = ctx.provenance.attempt_id()
    build = build_verdict(
        ctx.builder, reference_attempt,
        [{"path": f["path"], "b64": base64.b64encode(f["content"]).decode("ascii")}
         for f in original.files],
        baseline_strategy,
        Recipe(
            makefile=original.makefile,
            targets=({"role": TIMING_ROLE, "target": original.target,
                      "executable": original.executable},),
            source_patterns=original.source_patterns,
        ),
    )
    detail = {REFERENCE_KEY: original.sha256, "provenance": original.provenance,
              "reference_build": build.detail, "runs": [], "problems": []}
    # The reviewed original is what this verdict is a comparison against,
    # so it is a formal material rather than a note in the detail.
    materials = (Subject(kind="reference", sha256=original.sha256),)
    if build.verdict != PASS:
        detail["problems"].append(
            "original reference did not build under the reviewed baseline strategy"
        )
        return CheckResult(
            verdict=FAIL, detail=detail,
            reasons=(*detail["problems"], *build.reasons), materials=materials,
            binary_artifacts=tuple(
                artifact for record in build.build_records
                for artifact in record.binary_artifacts
            ),
            measures_other_binaries=True,
        )

    kept = []
    measured_identities = []
    for run in original.runs:
        report, problem = _one_run(
            ctx.builder, run, original, candidate.executable,
            reference_attempt, candidate_attempt, kept, measured_identities,
        )
        detail["runs"].append(report)
        if problem is not None:
            detail["problems"].append(problem)

    if not detail["problems"] and all(run[PASS] for run in detail["runs"]):
        return CheckResult(
            verdict=PASS, detail=detail, materials=materials, stores=tuple(kept),
            # The reviewed original was built here, in a workspace of its
            # own: this verdict names a binary the region's build never
            # produced, and saying so is what keeps that from reading as
            # a build that moved.
            measures_other_binaries=True,
            binary_artifacts=(
                *tuple(artifact for record in build.build_records
                       for artifact in record.binary_artifacts),
                *measured_identities,
            ),
        )
    return CheckResult(
        verdict=FAIL, detail=detail,
        reasons=tuple([
            *detail["problems"],
            *(f"{run['name']}: the two programs did not agree on every output"
              for run in detail["runs"] if not run[PASS]),
        ]),
        materials=materials,
        measures_other_binaries=True,
        binary_artifacts=(
            *tuple(artifact for record in build.build_records
                   for artifact in record.binary_artifacts),
            *measured_identities,
        ),
    )
