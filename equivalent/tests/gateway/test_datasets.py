"""Reading a code's visible dataset, and refusing one that has drifted.

The dataset and the code's manifest are two files a person edits
separately. These say what happens when they disagree: the load stops and
names the variable, rather than the disagreement turning up later as a
comparison against arrays of the wrong shape.
"""
from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from equivalent.capture import npy
from equivalent.gateway.datasets import load_visible_cases
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import VISIBLE_CASE, fixture_arrays, write_program


def _program(tmp_path):
    directory = write_program(tmp_path)
    return directory / "datasets" / "visible", load_manifest(directory / "manifest.yaml")


def test_every_listed_case_comes_back_as_the_bytes_on_disk(tmp_path):
    visible, manifest = _program(tmp_path)

    cases = load_visible_cases(visible, manifest)

    assert list(cases) == [VISIBLE_CASE]
    for variable, encoded in cases[VISIBLE_CASE].items():
        on_disk = npy.input_path(visible / VISIBLE_CASE, variable).read_bytes()
        assert base64.b64decode(encoded) == on_disk


def test_the_arrays_are_the_ones_the_case_directory_holds(tmp_path):
    visible, manifest = _program(tmp_path)

    cases = load_visible_cases(visible, manifest)

    for variable, expected in fixture_arrays().items():
        got = npy.decode(base64.b64decode(cases[VISIBLE_CASE][variable]))
        assert np.array_equal(got, expected)


def test_an_input_of_the_wrong_element_type_is_refused_and_named(tmp_path):
    visible, manifest = _program(tmp_path)
    wrong = manifest.interface.inputs[0]
    npy.input_path(visible / VISIBLE_CASE, wrong.name).write_bytes(
        npy.encode(np.zeros(4, dtype="<i4"))
    )

    with pytest.raises(ValueError) as caught:
        load_visible_cases(visible, manifest)

    assert wrong.name in str(caught.value)


def test_an_input_of_the_wrong_rank_is_refused_and_named(tmp_path):
    visible, manifest = _program(tmp_path)
    wrong = manifest.interface.inputs[1]
    npy.input_path(visible / VISIBLE_CASE, wrong.name).write_bytes(
        npy.encode(np.zeros(6, dtype="<f8"))
    )

    with pytest.raises(ValueError) as caught:
        load_visible_cases(visible, manifest)

    assert wrong.name in str(caught.value)


def test_a_case_missing_a_declared_input_is_refused_and_named(tmp_path):
    visible, manifest = _program(tmp_path)
    absent = manifest.interface.inputs[0].name
    kept = {k: v for k, v in fixture_arrays().items() if k != absent}
    npy.write_case(visible / VISIBLE_CASE, kept, {})
    npy.input_path(visible / VISIBLE_CASE, absent).unlink()

    with pytest.raises(ValueError) as caught:
        load_visible_cases(visible, manifest)

    assert absent in str(caught.value)


def test_a_case_holding_a_variable_the_code_does_not_declare_is_refused(tmp_path):
    visible, manifest = _program(tmp_path)
    extra = dict(fixture_arrays())
    extra["scratch"] = np.zeros(3, dtype="<f4")
    npy.write_case(visible / VISIBLE_CASE, extra, {})

    with pytest.raises(ValueError) as caught:
        load_visible_cases(visible, manifest)

    assert "scratch" in str(caught.value)


def test_a_dataset_listing_a_case_that_is_not_there_says_so(tmp_path):
    visible, manifest = _program(tmp_path)
    (visible / npy.CASES_FILE).write_text(json.dumps({"cases": [VISIBLE_CASE, "case0009"]}))

    with pytest.raises(FileNotFoundError):
        load_visible_cases(visible, manifest)
