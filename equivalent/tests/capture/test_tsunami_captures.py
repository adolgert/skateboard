"""The reference data this repository tracks reads as the capture format says.

These are the answers every regression verdict for the worked example is
made against, converted in place from an older raw-stream layout rather
than regenerated. If the conversion had lost the shape, the element type,
or a file, every later claim would be comparing against something else.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from equivalent.capture import npy
from equivalent.manifest.schema import load_manifest

CODE = Path(__file__).resolve().parents[3] / "programs" / "tsunami"
DATASETS = (
    CODE / "datasets" / "visible",
    CODE / "captures" / "visible",
    CODE / "captures" / "holdout",
)

# The array length the old cases.json recorded once per dataset, and the
# element type its raw streams were read with.
GRID = 100


@pytest.mark.parametrize("directory", DATASETS, ids=lambda d: f"{d.parent.name}/{d.name}")
def test_every_case_holds_exactly_the_files_its_case_json_lists(directory):
    for case in npy.dataset_cases(directory):
        listed = npy.read_case_names(directory / case)
        expected = (
            {npy.CASE_FILE}
            | {f"{name}{npy.INPUT_SUFFIX}" for name in listed["inputs"]}
            | {f"{name}{npy.OUTPUT_SUFFIX}" for name in listed["outputs"]}
        )

        assert {p.name for p in (directory / case).iterdir()} == expected, case


def test_a_converted_expected_output_is_the_array_the_old_layout_held():
    manifest = load_manifest(CODE / "manifest.yaml")
    case = npy.read_case(CODE / "captures" / "visible" / "case0000")

    for variable in manifest.interface.outputs:
        array = case["outputs"][variable.name]
        assert array.dtype == np.dtype(npy.NUMPY_DTYPE["f32"])
        assert array.shape == (GRID,)


@pytest.mark.parametrize("directory", DATASETS, ids=lambda d: f"{d.parent.name}/{d.name}")
def test_every_array_matches_the_variable_the_manifest_declares(directory):
    manifest = load_manifest(CODE / "manifest.yaml")
    declared = {
        "inputs": {v.name: v for v in manifest.interface.inputs},
        "outputs": {v.name: v for v in manifest.interface.outputs},
    }

    for case, arrays in npy.load_dataset(directory).items():
        for side in ("inputs", "outputs"):
            for name, array in arrays[side].items():
                npy.check(array, declared[side][name])
