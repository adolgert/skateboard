"""Loads a region's visible capture set from disk.

The layout a visible dataset directory holds: `<dir>/cases.json`
({"cases": [name, ...]}) plus, per case, a directory holding one
`<variable>.npy` per input and a `case.json` naming them.

Trust role: these bytes are the inputs every replay runs against. Loading
the wrong directory, or mangling a case, makes gpu/executed and
regression/visible claims describe a run of the wrong data. So each case
is read against the code's own manifest: the dataset must hold exactly
the input variables the region declares, each of the declared element
type and rank. A dataset that has drifted from the manifest stops the
gateway here, naming the variable, rather than reaching the builder and
failing as a puzzling comparison further along.
"""
from __future__ import annotations

import base64
from pathlib import Path

from equivalent.capture import npy
from equivalent.manifest.schema import Manifest


def load_visible_cases(dataset_dir, manifest: Manifest) -> dict:
    """{case: {variable: base64 of that variable's .npy file}} for every case.

    The file bytes travel as they are on disk, so what the builder writes
    into its case directory is byte-for-byte what this dataset holds.
    """
    dataset_dir = Path(dataset_dir)
    declared = {variable.name: variable for variable in manifest.interface.inputs}
    cases = {}
    for name in npy.dataset_cases(dataset_dir):
        directory = dataset_dir / name
        where = f"visible dataset case '{name}' in {dataset_dir}"
        listed = npy.read_case_names(directory)["inputs"]

        undeclared = sorted(set(listed) - set(declared))
        if undeclared:
            raise ValueError(
                f"{where} holds input variable(s) {undeclared}, which code "
                f"'{manifest.name}' does not declare; it declares {sorted(declared)}"
            )
        absent = sorted(set(declared) - set(listed))
        if absent:
            raise ValueError(
                f"{where} is missing input variable(s) {absent}, which code "
                f"'{manifest.name}' declares"
            )

        arrays = {}
        for variable in listed:
            data = npy.input_path(directory, variable).read_bytes()
            try:
                npy.check(npy.decode(data), declared[variable])
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from exc
            arrays[variable] = base64.b64encode(data).decode()
        cases[name] = arrays
    return cases
