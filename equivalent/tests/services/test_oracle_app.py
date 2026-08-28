"""The oracle's HTTP surface, over a small capture tree written here.

These read as the statement of what the oracle promises: it compares
every output its own captures hold, it never quietly drops one, it hands
out held-out inputs but no held-out detail, and it refuses to start at all
if a variable it would have to compare has no tolerance band.
"""
from __future__ import annotations

import base64
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from equivalent.capture import npy
from equivalent.tests.fakes import (
    FIXTURE_VARIABLES,
    PROGRAM_TOLERANCES,
    fixture_arrays,
    program_tolerances,
    write_program,
)
from services.oracle.app import create_app, policy_path_for

VISIBLE = ["case0000", "case0001"]
HOLDOUT = ["hcase0000"]


def _wire(arrays: dict) -> dict:
    return {name: base64.b64encode(npy.encode(a)).decode() for name, a in arrays.items()}


def _captures(root, offset: int = 0):
    """A captures tree of the shape the oracle image bakes in."""
    captures = root / "captures"
    for case in VISIBLE:
        npy.write_case(captures / "visible" / case, {}, fixture_arrays(offset))
    (captures / "visible" / npy.CASES_FILE).write_text(json.dumps({"cases": VISIBLE}))
    for case in HOLDOUT:
        npy.write_case(captures / "holdout" / case, fixture_arrays(), fixture_arrays(offset))
    (captures / "holdout" / npy.CASES_FILE).write_text(json.dumps({"cases": HOLDOUT}))
    return captures


def _client(tmp_path, tolerances=None):
    program = write_program(tmp_path)
    captures = _captures(tmp_path)
    if tolerances is not None:
        program_tolerances(program).write_text(json.dumps(tolerances))
    return TestClient(create_app(captures, program_tolerances(program), program / "manifest.yaml"))


def _submission(offset: int = 0) -> dict:
    return {case: _wire(fixture_arrays(offset)) for case in VISIBLE}


def test_healthz_counts_both_datasets(tmp_path):
    body = _client(tmp_path).get("/healthz").json()

    assert body["n_visible"] == len(VISIBLE)
    assert body["n_holdout"] == len(HOLDOUT)


def test_policy_reports_the_version_and_the_hash_of_the_file_it_read(tmp_path):
    body = _client(tmp_path).get("/v1/policy").json()

    assert body["policy_version"] == PROGRAM_TOLERANCES["policy_version"]
    assert len(body["policy_sha256"]) == 64


def test_the_expected_answers_compare_equal_to_themselves(tmp_path):
    body = _client(tmp_path).post(
        "/v1/compare", json={"dataset": "visible", "outputs": _submission()}
    ).json()

    assert body["verdict"] == "pass"
    assert set(body["per_case"]) == set(VISIBLE)
    assert all(v["pass"] for v in body["per_case"]["case0000"]["per_var"].values())


def test_a_submission_missing_a_declared_output_fails_and_names_it(tmp_path):
    dropped = FIXTURE_VARIABLES[0]["name"]
    outputs = _submission()
    for case in outputs:
        outputs[case].pop(dropped)

    body = _client(tmp_path).post(
        "/v1/compare", json={"dataset": "visible", "outputs": outputs}
    ).json()

    assert body["verdict"] == "fail"
    assert dropped in json.dumps(body["per_case"]["case0000"])


def test_a_variable_nobody_expected_is_listed_and_changes_nothing(tmp_path):
    outputs = _submission()
    for case in outputs:
        outputs[case]["scratch"] = _wire({"scratch": np.zeros(3, dtype="<f4")})["scratch"]

    body = _client(tmp_path).post(
        "/v1/compare", json={"dataset": "visible", "outputs": outputs}
    ).json()

    assert body["verdict"] == "pass"
    assert body["per_case"]["case0000"]["extra"] == ["scratch"]


def test_a_case_with_no_output_at_all_fails(tmp_path):
    body = _client(tmp_path).post(
        "/v1/compare", json={"dataset": "visible", "outputs": {}}
    ).json()

    assert body["verdict"] == "fail"
    assert body["per_case"]["case0000"]["pass"] is False


def test_a_wrong_answer_fails(tmp_path):
    body = _client(tmp_path).post(
        "/v1/compare", json={"dataset": "visible", "outputs": _submission(offset=100)}
    ).json()

    assert body["verdict"] == "fail"


def test_holdout_returns_a_verdict_and_no_per_case_detail(tmp_path):
    client = _client(tmp_path)
    outputs = {case: _wire(fixture_arrays()) for case in HOLDOUT}

    body = client.post("/v1/compare", json={"dataset": "holdout", "outputs": outputs}).json()

    assert body["verdict"] == "pass"
    assert "per_case" not in body


def test_holdout_inputs_are_served_and_expected_outputs_are_not(tmp_path):
    body = _client(tmp_path).get("/v1/dataset/holdout/inputs").json()

    case = body["cases"]["hcase0000"]
    assert sorted(case) == sorted(v["name"] for v in FIXTURE_VARIABLES)
    for name, blob in case.items():
        array = npy.decode(base64.b64decode(blob))
        assert np.array_equal(array, fixture_arrays()[name])


def test_an_unknown_dataset_is_refused(tmp_path):
    assert _client(tmp_path).post(
        "/v1/compare", json={"dataset": "made-up", "outputs": {}}
    ).status_code == 400


def test_startup_fails_when_a_float_output_has_no_tolerance_entry(tmp_path):
    missing = FIXTURE_VARIABLES[1]["name"]
    thinned = {
        **PROGRAM_TOLERANCES,
        "variables": {k: v for k, v in PROGRAM_TOLERANCES["variables"].items() if k != missing},
    }

    with pytest.raises(ValueError) as caught:
        _client(tmp_path, tolerances=thinned)

    assert missing in str(caught.value)


def test_startup_fails_when_a_tolerance_entry_is_incomplete(tmp_path):
    name = FIXTURE_VARIABLES[0]["name"]
    partial = {
        **PROGRAM_TOLERANCES,
        "variables": {**PROGRAM_TOLERANCES["variables"], name: {"abs": 1e-6}},
    }

    with pytest.raises(ValueError) as caught:
        _client(tmp_path, tolerances=partial)

    assert name in str(caught.value)


def test_startup_fails_when_a_dataset_is_missing(tmp_path):
    program = write_program(tmp_path)
    captures = tmp_path / "empty"
    (captures / "visible").mkdir(parents=True)

    with pytest.raises(ValueError) as caught:
        create_app(captures, program_tolerances(program), program / "manifest.yaml")

    assert "holdout" in str(caught.value)


def _not_ready_client(tmp_path, *, minimal: bool = True):
    """The oracle of a deployment whose code has not been brought in yet.

    There are no captures, and the manifest is still the minimal form, so
    it does not even say where a tolerance policy would be.
    """
    program = write_program(tmp_path, minimal=minimal)
    manifest_path = program / "manifest.yaml"
    return TestClient(create_app(
        program / "captures", policy_path_for(program, manifest_path), manifest_path,
    ))


def test_an_oracle_with_nothing_to_compare_still_starts_and_says_it_is_not_ready(tmp_path):
    # A deployment is brought up in order to produce the captures this
    # service is missing, so refusing to start would be refusing to start
    # the work.
    body = _not_ready_client(tmp_path).get("/healthz").json()

    assert body["ok"] is True
    assert body["ready"] is False
    assert body["n_visible"] == 0
    assert body["n_holdout"] == 0
    assert "captures" in body["missing"] and "tolerances" in body["missing"]


def test_an_oracle_that_is_not_ready_refuses_every_question_about_a_comparison(tmp_path):
    client = _not_ready_client(tmp_path)

    for response in (
        client.get("/v1/policy"),
        client.get("/v1/dataset/holdout/inputs"),
        client.post("/v1/compare", json={"dataset": "visible", "outputs": {}}),
    ):
        assert response.status_code == 409
        assert "captures" in response.json()["detail"]


def test_a_complete_manifest_says_where_its_tolerance_policy_is(tmp_path):
    # Inside the source tree, not beside the manifest: every path a
    # manifest names other than its source root is read from the tree.
    program = write_program(tmp_path)

    found = policy_path_for(program, program / "manifest.yaml")

    assert found == program_tolerances(program)
    assert found.is_file()
