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
from equivalent.ledger.packed import PackedArtifact
from equivalent.ledger.subjects import Subject, hash_bytes
from equivalent.reference.schema import load_reference
from equivalent.tree import attempt_id_for_strategy

from .build_replay import fortran_of
from .context import CheckContext, CheckResult, failed
from .errors import ComponentError, after_the_manifest_check_passed


def _comparison(expected: bytes, actual: bytes, spec: dict) -> dict:
    if spec["comparison"] == "bytes":
        return {"pass": expected == actual}
    try:
        ref, got = npy.decode(expected), npy.decode(actual)
        if ref.dtype.kind not in "fibu" or got.dtype.kind not in "fibu":
            return {"pass": False, "error": "reference arrays must have numeric or logical dtype"}
        tolerance = spec.get("tolerance", {"abs": 0, "rel": 0, "ulp": 0})
        return compare_variable(ref, got, tolerance)
    except Exception as exc:
        return {"pass": False, "error": f"cannot compare reference arrays: {exc}"}


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


def check(ctx: CheckContext, config: dict) -> CheckResult:
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
    candidate = manifest.build.targets.get("timing")
    if candidate is None:
        problems = ["candidate declares no timing target"]
        return failed({"problems": problems}, problems)
    baseline_strategy = ctx.baseline_strategy
    builder = ctx.builder
    compiler = fortran_of(baseline_strategy)
    reference_attempt = attempt_id_for_strategy(
        ctx.region_id + "-original", original.sha256, baseline_strategy.name,
    )
    candidate_attempt = ctx.provenance.attempt_id()
    payload = [{"path": f["path"], "b64": base64.b64encode(f["content"]).decode("ascii")}
               for f in original.files]
    try:
        build = builder.build(
            reference_attempt, payload, original.makefile,
            [{"role": "timing", "target": original.target, "executable": original.executable}],
            compiler.compiler, list(compiler.flags), list(baseline_strategy.link_flags),
            list(original.source_patterns),
        )
    except Exception as exc:
        raise ComponentError(f"original reference build failed: {exc}") from exc
    detail = {"reference_sha256": original.sha256, "provenance": original.provenance,
              "reference_build": build, "runs": [], "problems": []}
    # The reviewed original is what this verdict is a comparison against,
    # so it is a formal material rather than a note in the detail.
    materials = (Subject(kind="reference", sha256=original.sha256),)
    if (build.get("ok") is not True or build.get("flags_reached_every_compile") is not True
            or build.get("compiled_only_tree_source") is not True):
        detail["problems"].append("original reference did not build under the reviewed baseline strategy")
        return CheckResult(
            verdict="fail", detail=detail, reasons=tuple(detail["problems"]),
            materials=materials,
        )
    kept = []
    for run in original.runs:
        original_outputs = sorted({o["original"] for o in run["outputs"]})
        candidate_outputs = sorted({o["candidate"] for o in run["outputs"]})
        try:
            expected = builder.time(reference_attempt, original.executable, run["original_args"],
                                    run["env"], original_outputs, 2, run["budget_s"])
            actual = builder.time(candidate_attempt, candidate.executable, run["candidate_args"],
                                  run["env"], candidate_outputs, 2, run["budget_s"])
        except Exception as exc:
            raise ComponentError(f"original comparison run {run['name']!r} could not finish: {exc}") from exc
        report = {"name": run["name"], "outputs": [], "pass": False,
                  "original_runs_s": expected.get("runs_s", []),
                  "candidate_runs_s": actual.get("runs_s", []),
                  "original_binary": {"executable_identity": expected.get("executable_identity")},
                  "candidate_binary": {"executable_identity": actual.get("executable_identity")}}
        detail["runs"].append(report)
        if any(r.get("ok") is not True or len(r.get("outputs", [])) != 2
               or len(r.get("runs_s", [])) != 2 for r in (expected, actual)):
            detail["problems"].append(f"{run['name']}: both programs must complete two runs")
            continue
        for output in run["outputs"]:
            result = {"original": output["original"], "candidate": output["candidate"],
                      "comparison": output["comparison"], "pass": False}
            report["outputs"].append(result)
            try:
                reference_files = [_artifact(kept, r[output["original"]]) for r in expected["outputs"]]
                candidate_files = [_artifact(kept, r[output["candidate"]]) for r in actual["outputs"]]
                result["original_artifacts"] = [f[1] for f in reference_files]
                result["candidate_artifacts"] = [f[1] for f in candidate_files]
                result["deterministic"] = (reference_files[0][0] == reference_files[1][0]
                                           and candidate_files[0][0] == candidate_files[1][0])
                result["comparison_result"] = _comparison(reference_files[0][0], candidate_files[0][0], output)
                result["pass"] = result["deterministic"] and result["comparison_result"]["pass"]
            except (KeyError, ValueError, TypeError) as exc:
                result["error"] = f"missing or malformed program output: {exc}"
        report["pass"] = all(o["pass"] for o in report["outputs"])
    passed = not detail["problems"] and all(run["pass"] for run in detail["runs"])
    if passed:
        return CheckResult(
            verdict="pass", detail=detail, materials=materials, stores=tuple(kept),
        )
    return CheckResult(
        verdict="fail", detail=detail,
        reasons=tuple([
            *detail["problems"],
            *(f"{run['name']}: the two programs did not agree on every output"
              for run in detail["runs"] if not run["pass"]),
        ]),
        materials=materials,
        stores=tuple(kept),
    )
