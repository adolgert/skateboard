"""Reads and writes one case of captured arrays, in NPY.

A case is a directory. Every input variable is `<name>.npy`, every output
variable is `<name>.out.npy`, and `case.json` lists the two sets of names.
A dataset is a directory of case directories plus `cases.json`, which
lists them. The NPY file carries its own dtype, shape, and column-major
flag, so nothing outside the file has to be told what a variable looks
like -- which is the point: no variable name and no element type is
written down in this package.

Trust role: these bytes are the inputs every replay runs against and the
answers every comparison is made against. If this module wrote a
transposed array, or read one back as the wrong type, a port that is
wrong would compare equal to a reference that is also wrong in the same
way, and every regression claim above it would be describing arrays
nobody produced. `check` is the other half of that: it refuses an array
whose type or rank disagrees with the variable the code's manifest
declares, so a dataset that drifted from the manifest is caught where it
is read rather than in a puzzling comparison later.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np

from equivalent.manifest.schema import Variable

# How the manifest's dtype names spell themselves for numpy. Explicit
# byte order, because these files are read on a machine other than the
# one that wrote them; `|b1` (one byte per element) is how a Fortran
# logical is stored, whatever width the compiler gives it in memory.
NUMPY_DTYPE = {
    "f32": "<f4",
    "f64": "<f8",
    "i32": "<i4",
    "i64": "<i8",
    "l": "|b1",
}

CASE_FILE = "case.json"
CASES_FILE = "cases.json"
INPUT_SUFFIX = ".npy"
OUTPUT_SUFFIX = ".out.npy"


def encode(array) -> bytes:
    """One array as the bytes of an NPY file."""
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(array), allow_pickle=False)
    return buffer.getvalue()


def decode(data: bytes):
    """The array an NPY file's bytes hold."""
    return np.load(io.BytesIO(data), allow_pickle=False)


def check(array, variable: Variable) -> None:
    """Refuse an array that is not what the manifest says this variable is."""
    expected = np.dtype(NUMPY_DTYPE[variable.dtype])
    array = np.asarray(array)
    if array.dtype != expected:
        raise ValueError(
            f"variable '{variable.name}' is declared {variable.dtype} ({expected.str}) "
            f"but the array read is {array.dtype.str}"
        )
    if array.ndim != variable.rank:
        raise ValueError(
            f"variable '{variable.name}' is declared rank {variable.rank} "
            f"but the array read has rank {array.ndim} (shape {array.shape})"
        )


def input_path(directory, name: str) -> Path:
    return Path(directory) / f"{name}{INPUT_SUFFIX}"


def output_path(directory, name: str) -> Path:
    return Path(directory) / f"{name}{OUTPUT_SUFFIX}"


def read_case_names(directory) -> dict:
    """The input and output variable names `case.json` lists, in order."""
    directory = Path(directory)
    listed = json.loads((directory / CASE_FILE).read_text())
    return {"inputs": list(listed["inputs"]), "outputs": list(listed["outputs"])}


def _read(path: Path, name: str):
    if not path.is_file():
        raise ValueError(f"variable '{name}' is listed in {CASE_FILE} but {path} does not exist")
    return decode(path.read_bytes())


def read_case(directory) -> dict:
    """{"inputs": {name: array}, "outputs": {name: array}} for one case directory.

    Every name `case.json` lists must have its file: a directory holds
    exactly what it says it holds. A visible dataset lists inputs and no
    outputs, a visible capture set lists outputs and no inputs, and a
    held-out capture set lists both.
    """
    directory = Path(directory)
    names = read_case_names(directory)
    return {
        "inputs": {n: _read(input_path(directory, n), n) for n in names["inputs"]},
        "outputs": {n: _read(output_path(directory, n), n) for n in names["outputs"]},
    }


def write_case(directory, inputs: dict, outputs: dict) -> None:
    """Write one case directory: its arrays and the `case.json` listing them."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, array in inputs.items():
        input_path(directory, name).write_bytes(encode(array))
    for name, array in outputs.items():
        output_path(directory, name).write_bytes(encode(array))
    (directory / CASE_FILE).write_text(
        json.dumps({"inputs": sorted(inputs), "outputs": sorted(outputs)}, indent=2) + "\n"
    )


def dataset_cases(directory) -> list:
    """The case names `cases.json` lists, in the order it lists them."""
    return list(json.loads((Path(directory) / CASES_FILE).read_text())["cases"])


def load_dataset(directory) -> dict:
    """{case: {"inputs": {...}, "outputs": {...}}} for a whole dataset directory."""
    directory = Path(directory)
    return {name: read_case(directory / name) for name in dataset_cases(directory)}
