#!/usr/bin/env python3
"""Rewrites this code's tracked capture files from raw streams to NPY.

The reference data was generated once, by one compiler, and regenerating
it with another one would move every expected answer. So the files are
converted in place instead: the same bytes, re-headed. This script is the
provenance of the converted files -- it says exactly what the old layout
was and how each old file became a new one -- and is kept for that
reason, not because it is expected to run again.

The old layout: one raw little-endian float32 stream per variable per
case, named `<variable>_in.bin` and `<variable>_out.bin`, with the array
length recovered from the file size and the grid size repeated in
cases.json. The new layout: `<variable>.npy` and `<variable>.out.npy`,
each self-describing, plus a case.json naming what the directory holds.

Usage: python3 convert_bin_to_npy.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
REPO = CODE.parent.parent
sys.path.insert(0, str(REPO))

from equivalent.capture import npy  # noqa: E402  (found through the path added above)

# Every directory of cases this code tracks. A visible dataset holds
# inputs, the matching capture set holds the expected outputs, and the
# held-out set holds both.
DATASETS = (
    CODE / "datasets" / "visible",
    CODE / "captures" / "visible",
    CODE / "captures" / "holdout",
)

# What the arrays in those files are. The old format said this nowhere,
# which is the reason for the conversion.
OLD_DTYPE = "<f4"
OLD_INPUT = "_in.bin"
OLD_OUTPUT = "_out.bin"


def convert_case(directory: Path, dry_run: bool) -> dict:
    """One case directory: write the NPY files, drop the raw ones."""
    names = {"inputs": [], "outputs": []}
    for old in sorted(directory.glob("*.bin")):
        if old.name.endswith(OLD_INPUT):
            variable = old.name[: -len(OLD_INPUT)]
            new = npy.input_path(directory, variable)
            names["inputs"].append(variable)
        elif old.name.endswith(OLD_OUTPUT):
            variable = old.name[: -len(OLD_OUTPUT)]
            new = npy.output_path(directory, variable)
            names["outputs"].append(variable)
        else:
            raise ValueError(f"{old} is not named like an input or an output")
        array = np.fromfile(old, dtype=OLD_DTYPE)
        print(f"  {old.name} -> {new.name}  {array.dtype.str} {array.shape}")
        if not dry_run:
            new.write_bytes(npy.encode(array))
            old.unlink()
    if not dry_run:
        (directory / npy.CASE_FILE).write_text(json.dumps(names, indent=2) + "\n")
    return names


def convert_dataset(directory: Path, dry_run: bool) -> None:
    print(f"{directory}")
    listed = json.loads((directory / npy.CASES_FILE).read_text())
    cases = sorted(listed["cases"])
    for case in cases:
        convert_case(directory / case, dry_run)
    # grid_size went away with the raw streams: the shape is in the file.
    if not dry_run:
        (directory / npy.CASES_FILE).write_text(json.dumps({"cases": cases}, indent=2) + "\n")


def main(argv: list) -> int:
    dry_run = "--dry-run" in argv[1:]
    for directory in DATASETS:
        convert_dataset(directory, dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
