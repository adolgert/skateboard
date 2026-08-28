"""Examples of reading and writing one case of captured arrays.

Every dtype and rank the manifest may declare goes out to bytes and comes
back with its values, its shape, and its Fortran ordering intact; a case
directory round-trips; and an array that disagrees with the variable the
manifest declared is refused by name.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from equivalent.capture import npy
from equivalent.manifest.schema import DTYPES, MAX_RANK, Variable

# Deliberately unequal extents, so an array written in the wrong order
# would come back with the wrong shape rather than silently transposed.
SHAPES = {0: (), 1: (5,), 2: (2, 3), 3: (2, 3, 4), 4: (2, 3, 4, 5)}


def _sample(dtype: str, rank: int) -> np.ndarray:
    """A distinct value in every element, laid out the way Fortran lays it out."""
    shape = SHAPES[rank]
    n = int(np.prod(shape, dtype=int))
    values = np.arange(1, n + 1)
    if dtype == "l":
        values = (values % 3) == 0
    return np.asarray(values, dtype=npy.NUMPY_DTYPE[dtype]).reshape(shape, order="F")


@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("rank", range(MAX_RANK + 1))
def test_round_trip_keeps_values_shape_and_fortran_order(dtype, rank):
    array = _sample(dtype, rank)

    back = npy.decode(npy.encode(array))

    assert back.dtype == np.dtype(npy.NUMPY_DTYPE[dtype])
    assert back.shape == array.shape
    assert np.array_equal(back, array)
    assert back.flags.f_contiguous


def test_a_rank_two_file_says_fortran_order_in_its_own_header():
    # The bytes are what a Fortran writer has to match, so this asserts on
    # the file rather than on what numpy hands back.
    header = npy.encode(_sample("f64", 2))[:80]

    assert header.startswith(b"\x93NUMPY")
    assert b"'fortran_order': True" in header
    assert b"'descr': '<f8'" in header


def test_check_accepts_the_variable_the_manifest_declared():
    npy.check(_sample("f32", 2), Variable(name="field", dtype="f32", rank=2))


def test_check_rejects_a_wrong_dtype_and_names_the_variable():
    with pytest.raises(ValueError) as caught:
        npy.check(_sample("f64", 1), Variable(name="field", dtype="f32", rank=1))

    assert "field" in str(caught.value)


def test_check_rejects_a_wrong_rank_and_names_the_variable():
    with pytest.raises(ValueError) as caught:
        npy.check(_sample("f32", 1), Variable(name="field", dtype="f32", rank=2))

    assert "field" in str(caught.value)


def test_a_case_directory_round_trips(tmp_path):
    inputs = {"field": _sample("f32", 1), "flag": _sample("l", 0)}
    outputs = {"field": _sample("f32", 1), "count": _sample("i64", 2)}

    npy.write_case(tmp_path, inputs, outputs)
    back = npy.read_case(tmp_path)

    assert sorted(back["inputs"]) == ["field", "flag"]
    assert sorted(back["outputs"]) == ["count", "field"]
    assert np.array_equal(back["outputs"]["count"], outputs["count"])
    assert back["outputs"]["count"].flags.f_contiguous


def test_a_case_directory_holds_exactly_the_files_its_case_json_lists(tmp_path):
    npy.write_case(tmp_path, {"field": _sample("f32", 1)}, {"field": _sample("f32", 1)})

    listed = json.loads((tmp_path / npy.CASE_FILE).read_text())

    assert listed == {"inputs": ["field"], "outputs": ["field"]}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        npy.CASE_FILE, "field.npy", "field.out.npy",
    ]


def test_a_case_missing_a_file_its_case_json_lists_is_refused(tmp_path):
    npy.write_case(tmp_path, {"field": _sample("f32", 1)}, {})
    (tmp_path / "field.npy").unlink()

    with pytest.raises(ValueError) as caught:
        npy.read_case(tmp_path)

    assert "field" in str(caught.value)


def test_a_dataset_directory_loads_every_case_it_lists(tmp_path):
    for name in ("case0000", "case0001"):
        (tmp_path / name).mkdir()
        npy.write_case(tmp_path / name, {"field": _sample("f32", 1)}, {})
    (tmp_path / npy.CASES_FILE).write_text(json.dumps({"cases": ["case0000", "case0001"]}))

    dataset = npy.load_dataset(tmp_path)

    assert sorted(dataset) == ["case0000", "case0001"]
    assert dataset["case0000"]["inputs"]["field"].shape == (5,)
    assert dataset["case0000"]["outputs"] == {}
