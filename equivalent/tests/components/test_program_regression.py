"""Comparing a ported program's own whole-program outputs against the baseline's.

This is the check that says a port is still the same code at the size it
is timed at, so these read as the statement of what that means: the files
the ported program writes are compared, file by file, against the files
the baseline program wrote, under the code's own tolerance policy, and
anything that cannot be compared is a failure naming what it was.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from equivalent.components import program_regression
from equivalent.components.errors import ComponentError
from equivalent.ledger.capture_sets import program_variable, store_program_set
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import (
    FakeBuilder,
    program_tolerances,
    timing_array,
    write_program,
)

TREE = Subject(kind="tree", sha256="1" * 64)
BASELINE_TREE = Subject(kind="tree", sha256="2" * 64)


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


def _store_with_baseline(tmp_path, manifest, *, shift: float = 0.0, detail=None):
    """A ledger holding one passing baseline timing claim and the set it stored."""
    store = LedgerStore(tmp_path / "region")
    if detail is None:
        subject = store_program_set(store, _baseline_arrays(manifest, shift))
        detail = {"program_set": subject.sha256}
    store.record_claim(
        [BASELINE_TREE], "timing/baseline",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass", detail=detail),
        [], "sess-1",
    )
    return store


def test_a_port_that_writes_what_the_baseline_wrote_passes(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest)

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
    )

    assert result["verdict"] == "pass"
    assert sorted(result["detail"]["per_var"]) == sorted(
        program_variable(path) for path in manifest.timing.outputs
    )
    assert all(entry["pass"] for entry in result["detail"]["per_var"].values())
    # The run is still a run of the program, so what it cost is recorded.
    assert result["detail"]["runs_s"]


def test_the_program_is_run_once_with_what_the_manifest_declares(tmp_path):
    # One run: this is a comparison, and the measurement is time_port's job.
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest)
    builder = FakeBuilder()

    program_regression.check(store, BASELINE_TREE, "ch04:step", "tree123", manifest, builder)

    call = builder.time_calls[0]
    assert call["executable"] == manifest.build.targets["timing"].executable
    assert call["args"] == list(manifest.timing.args)
    assert call["env"] == dict(manifest.timing.env)
    assert call["outputs"] == list(manifest.timing.outputs)
    assert call["repeats"] == 1


def test_an_element_outside_the_band_fails_and_names_the_output(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest, shift=1.0)

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
    )

    assert result["verdict"] == "fail"
    failed = [name for name, entry in result["detail"]["per_var"].items() if not entry["pass"]]
    assert failed == sorted(program_variable(p) for p in manifest.timing.outputs)
    # And by how much, so a person can see whether it is a rounding
    # difference or a different answer.
    assert result["detail"]["per_var"][failed[0]]["max_abs"] == 1.0


def test_an_output_the_ported_program_did_not_write_fails_and_names_it(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest)
    missing = manifest.timing.outputs[0]

    class Forgetful(FakeBuilder):
        def timing_outputs(self, outputs, run: int) -> dict:
            written = super().timing_outputs(outputs, run)
            del written[missing]
            return written

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, Forgetful(),
    )

    assert result["verdict"] == "fail"
    assert missing in result["detail"]["per_var"][program_variable(missing)]["error"]


def test_an_output_of_a_different_shape_fails(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest)
    shorter = manifest.timing.outputs[0]

    class Truncating(FakeBuilder):
        def timing_outputs(self, outputs, run: int) -> dict:
            import base64

            from equivalent.capture import npy
            written = super().timing_outputs(outputs, run)
            written[shorter] = base64.b64encode(
                npy.encode(timing_array(shorter)[:-1])
            ).decode()
            return written

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, Truncating(),
    )

    assert result["verdict"] == "fail"
    assert "shape" in result["detail"]["per_var"][program_variable(shorter)]["error"]


def test_with_no_baseline_timing_claim_it_says_to_time_the_baseline_first(tmp_path):
    store = LedgerStore(tmp_path / "region")

    with pytest.raises(ComponentError) as excinfo:
        program_regression.check(
            store, BASELINE_TREE, "ch04:step", "tree123", _manifest(tmp_path), FakeBuilder(),
        )

    assert "time_baseline" in str(excinfo.value)


def test_a_baseline_claim_that_stored_no_set_is_not_a_reference(tmp_path):
    # The claim passed -- the program was timed -- but it left nothing to
    # compare against, so there is still nothing to do this check with.
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest, detail={"program_set": None})

    with pytest.raises(ComponentError) as excinfo:
        program_regression.check(
            store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
        )

    assert "time_baseline" in str(excinfo.value)


def test_the_latest_baseline_set_is_the_one_compared_against(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest, shift=1.0)
    # A second baseline run, of a program that now writes what this port
    # writes: the newer claim is the reference.
    _store_with_baseline(tmp_path, manifest)

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
    )

    assert result["verdict"] == "pass"


def test_a_float_output_with_no_band_fails_and_names_it(tmp_path):
    # The gateway refuses such a manifest during onboarding; if one ever
    # reaches here, the answer is not a comparison made up on the spot.
    directory = write_program(tmp_path)
    policy_path = program_tolerances(directory)
    policy = json.loads(policy_path.read_text())
    unbanded = "results/flux.npy"
    del policy["files"][unbanded]
    policy_path.write_text(json.dumps(policy))
    manifest = load_manifest(directory / "manifest.yaml")
    store = _store_with_baseline(tmp_path, manifest)

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
    )

    assert result["verdict"] == "fail"
    assert unbanded in result["detail"]["per_var"][program_variable(unbanded)]["error"]


def test_the_claim_can_name_the_policy_and_the_set_it_was_judged_against(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest)
    policy_sha = program_regression.tolerance_policy(manifest)[1]

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
    )

    assert result["detail"]["policy_sha256"] == policy_sha
    assert len(result["detail"]["program_set"]) == 64


def test_a_timing_run_that_does_not_finish_is_a_verdict_and_not_an_error(tmp_path):
    manifest = _manifest(tmp_path)
    store = _store_with_baseline(tmp_path, manifest)
    builder = FakeBuilder()
    builder.time_ok = False

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, builder,
    )

    assert result["verdict"] == "fail"
    # Still named, so the claim rests on the same two things a passing one does.
    assert result["detail"]["program_set"]
    assert result["detail"]["policy_sha256"]


def test_an_integer_output_is_compared_exactly(tmp_path):
    # Nothing here is told what type a file holds; the file says, and a
    # band is consulted only for the types that are measurements.
    manifest = _manifest(tmp_path)
    counts = program_variable(manifest.timing.outputs[0])
    store = LedgerStore(tmp_path / "region")
    arrays = _baseline_arrays(manifest)
    arrays[counts] = np.asarray([1, 2, 3], dtype="<i4")
    subject = store_program_set(store, arrays)
    store.record_claim(
        [BASELINE_TREE], "timing/baseline",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass",
                  detail={"program_set": subject.sha256}),
        [], "sess-1",
    )

    result = program_regression.check(
        store, BASELINE_TREE, "ch04:step", "tree123", manifest, FakeBuilder(),
    )

    assert result["verdict"] == "fail"
    assert "dtype" in result["detail"]["per_var"][counts]["error"]
