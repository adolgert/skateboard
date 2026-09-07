"""Comparing a ported program's own whole-program outputs against the baseline's.

This is the check that says a port is still the same code at the size it
is timed at, so these read as the statement of what that means: the files
the ported program writes are compared, file by file, against the files
the baseline program wrote, under the code's own tolerance policy, and
anything that cannot be compared is a failure naming what it was.
"""
from __future__ import annotations

import json
from functools import partial

import numpy as np
import pytest

from equivalent.components import program_regression
from equivalent.components.errors import ComponentError
from equivalent.components.answers import TimeResponse
from equivalent.components.program_outputs import program_variable
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import (
    FakeBuilder,
    keep_program_set,
    program_tolerances,
    timed,
    timing_array,
    timing_files,
    write_program,
)


def _manifest(tmp_path):
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def _baseline_arrays(manifest, shift: float = 0.0) -> dict:
    """What the baseline program wrote, as the stored set holds it.

    Shifting every element is how a test asks for a port that computes
    something else at the timing size.
    """
    return {
        program_variable(path): timing_array(path) + shift
        for path in manifest.timing.outputs
    }


def _with_baseline(harness, manifest, *, shift: float = 0.0, detail=None):
    """A region whose baseline has been timed, and left the set it stored."""
    if detail is None:
        detail = {
            "program_set": keep_program_set(harness.store, _baseline_arrays(manifest, shift)),
        }
    harness.claim("timing/baseline", detail)
    return harness


def _check(harness, manifest, *, builder=None):
    harness.repo()
    return program_regression.check(
        harness.context(region_id="ch04:step", phase="porting", manifest=manifest,
                        builder=builder or harness.builder),
        {},
    )


def test_a_port_that_writes_what_the_baseline_wrote_passes(harness):
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest)

    result = _check(harness, manifest)

    assert result.verdict == "pass"
    assert sorted(result.detail["per_var"]) == sorted(
        program_variable(path) for path in manifest.timing.outputs
    )
    assert all(entry["pass"] for entry in result.detail["per_var"].values())
    # The run is still a run of the program, so what it cost is recorded.
    assert result.detail["runs_s"]


def test_the_program_is_run_once_with_what_the_manifest_declares(harness):
    # One run: this is a comparison, and the measurement is time_port's job.
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest)
    builder = FakeBuilder()

    _check(harness, manifest, builder=builder)

    call = builder.time_calls[0]
    assert call["executable"] == manifest.build.targets["timing"].executable
    assert call["args"] == list(manifest.timing.args)
    assert call["env"] == dict(manifest.timing.env)
    assert call["outputs"] == list(manifest.timing.outputs)
    assert call["repeats"] == 1


def test_an_element_outside_the_band_fails_and_names_the_output(harness):
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest, shift=1.0)

    result = _check(harness, manifest)

    assert result.verdict == "fail"
    failed = [name for name, entry in result.detail["per_var"].items() if not entry["pass"]]
    assert failed == sorted(program_variable(p) for p in manifest.timing.outputs)
    # And by how much, so a person can see whether it is a rounding
    # difference or a different answer.
    assert result.detail["per_var"][failed[0]]["max_abs"] == 1.0


def test_a_run_missing_a_declared_output_is_refused_before_anything_is_compared(harness):
    # There is no comparison to report: a run that did not write what the
    # manifest declares is not a measurement, which is the same answer a
    # port's own timing gets for the same run.
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest)
    missing = manifest.timing.outputs[0]

    def forgetful(paths, run: int) -> dict:
        return {path: data for path, data in timing_files(paths, run).items()
                if path != missing}

    result = _check(harness, manifest,
                    builder=FakeBuilder(time=partial(timed, files=forgetful)))

    assert result.verdict == "fail"
    assert "per_var" not in result.detail
    assert any("declared output" in problem for problem in result.detail["problems"])
    # The refusal still says what the comparison would have rested on.
    assert [material.kind for material in result.materials] == ["policy", "capture_set"]


def test_an_output_of_a_different_shape_fails(harness):
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest)
    shorter = manifest.timing.outputs[0]

    def truncating(paths, run: int) -> dict:
        import base64

        from equivalent.capture import npy
        written = timing_files(paths, run)
        written[shorter] = base64.b64encode(
            npy.encode(timing_array(shorter)[:-1])
        ).decode()
        return written

    result = _check(harness, manifest,
                    builder=FakeBuilder(time=partial(timed, files=truncating)))

    assert result.verdict == "fail"
    assert "shape" in result.detail["per_var"][program_variable(shorter)]["error"]


def test_a_baseline_claim_that_stored_no_set_is_not_a_reference(harness):
    # The claim passed -- the program was timed -- but it left nothing to
    # compare against, so there is still nothing to do this check with.
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest, detail={"program_set": None})

    with pytest.raises(ComponentError) as excinfo:
        _check(harness, manifest)

    assert program_regression.BASELINE_PREDICATE in str(excinfo.value)


def test_the_latest_baseline_set_is_the_one_compared_against(harness):
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest, shift=1.0)
    # A second baseline run, of a program that now writes what this port
    # writes: the newer claim is the reference.
    _with_baseline(harness, manifest)

    result = _check(harness, manifest)

    assert result.verdict == "pass"


def test_a_float_output_with_no_band_fails_and_names_it(harness):
    # The gateway refuses such a manifest during onboarding; if one ever
    # reaches here, the answer is not a comparison made up on the spot.
    directory = write_program(harness.tmp_path)
    policy_path = program_tolerances(directory)
    policy = json.loads(policy_path.read_text())
    unbanded = "results/flux.npy"
    del policy["files"][unbanded]
    policy_path.write_text(json.dumps(policy))
    manifest = load_manifest(directory / "manifest.yaml")
    _with_baseline(harness, manifest)

    result = _check(harness, manifest)

    assert result.verdict == "fail"
    assert unbanded in result.detail["per_var"][program_variable(unbanded)]["error"]


def test_the_claim_can_name_the_policy_and_the_set_it_was_judged_against(harness):
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest)
    policy = program_regression.tolerance_policy(manifest)[1]

    result = _check(harness, manifest)

    assert result.detail["policy_sha256"] == policy.sha256
    assert len(result.detail["program_set"]) == 64


def test_a_timing_run_that_does_not_finish_is_a_verdict_and_not_an_error(harness):
    manifest = _manifest(harness.tmp_path)
    _with_baseline(harness, manifest)
    builder = FakeBuilder(time=TimeResponse(ok=False, log_tail="timing binary not built"))

    result = _check(harness, manifest, builder=builder)

    assert result.verdict == "fail"
    # Still named, so the claim rests on the same two things a passing one does.
    assert result.detail["program_set"]
    assert result.detail["policy_sha256"]


def test_an_integer_output_is_compared_exactly(harness):
    # Nothing here is told what type a file holds; the file says, and a
    # band is consulted only for the types that are measurements.
    manifest = _manifest(harness.tmp_path)
    counts = program_variable(manifest.timing.outputs[0])
    arrays = _baseline_arrays(manifest)
    arrays[counts] = np.asarray([1, 2, 3], dtype="<i4")
    _with_baseline(harness, manifest,
                   detail={"program_set": keep_program_set(harness.store, arrays)})

    result = _check(harness, manifest)

    assert result.verdict == "fail"
    assert "dtype" in result.detail["per_var"][counts]["error"]
