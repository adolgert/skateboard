"""Capture sets kept in a region's ledger, named by a hash of their bytes.

A capture set is a dataset of cases: the inputs a run of the code's own
capture program dumped at the region's call site, and the outputs it
dumped one call later. It is stored as the same directory layout the
capture reader already reads -- one directory per case, one NPY file per
variable -- so a person reviewing a ledger opens files rather than
unpacking an archive.

Trust role: a capture set is what every later comparison is made
against. Two things have to hold. The bytes that come back must be the
bytes that went in, or a replay would be judged against arrays nobody
captured. And the name must be a hash of the content alone, because that
is the whole of how a repeat capture is recognised as the set already
stored: same bytes, same subject, nothing else consulted. Nothing but
the files themselves goes into the hash -- not the dataset's name, not
where it was stored, not when -- so a set captured in one region and a
set captured in another are the same subject when they hold the same
arrays.
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
from pathlib import Path

from equivalent.capture import npy
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, hash_files

# Where capture sets live inside a region's artifacts directory. One
# subdirectory per set, named by the set's own hash.
CAPTURE_SETS = "capture_sets"

# What a timing run's own outputs are stored as: one dataset holding one
# case, whose variables are the files the program wrote. The baseline
# stores such a set and a port is compared against it, so both sides name
# it the same way here rather than each spelling it for itself.
PROGRAM_SET = "program"


def capture_sets_dir(store: LedgerStore) -> Path:
    directory = store.region_dir / "artifacts" / CAPTURE_SETS
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def capture_set_dir(store: LedgerStore, sha256: str) -> Path:
    """Where one capture set's cases sit. It may not exist yet."""
    return capture_sets_dir(store) / sha256


def write_dataset(directory: Path, cases: dict, *, inputs: bool = True, outputs: bool = True) -> None:
    """The dataset layout for these cases: every case, then the list of them.

    `inputs` and `outputs` say which half of each case is written. A
    stored capture set holds both; a dataset the agent is given holds the
    inputs alone, and the answers it is judged against hold the outputs
    alone. Writing a half leaves a case that says it holds only that
    half, which is what the reader expects to find there.
    """
    for name in sorted(cases):
        case = cases[name]
        npy.write_case(
            directory / name,
            case.get("inputs", {}) if inputs else {},
            case.get("outputs", {}) if outputs else {},
        )
    (directory / npy.CASES_FILE).write_text(
        json.dumps({"cases": sorted(cases)}, indent=2) + "\n"
    )


def _files_under(directory: Path) -> list[dict]:
    """Every file in the set, as (path relative to the set, bytes) pairs."""
    return [
        {"path": path.relative_to(directory).as_posix(), "content": path.read_bytes()}
        for path in sorted(directory.rglob("*")) if path.is_file()
    ]


def store_capture_set(store: LedgerStore, name: str, cases: dict) -> Subject:
    """Write one dataset of cases into the region's artifacts and name it.

    `cases` is {case: {"inputs": {variable: array}, "outputs": {...}}},
    which is what the capture reader hands back for a dataset directory.
    `name` is what the manifest calls this dataset; it is for the caller's
    own messages and is deliberately not part of the hash, so that two
    datasets holding the same arrays are one artifact rather than two
    copies that a later comparison would have to know are the same.

    Storing a set that is already there writes nothing and returns the
    same subject.
    """
    staging = Path(tempfile.mkdtemp(dir=capture_sets_dir(store), prefix=".staging-"))
    try:
        write_dataset(staging, cases)
        subject = Subject(kind="capture_set", sha256=hash_files(_files_under(staging)))
        destination = capture_set_dir(store, subject.sha256)
        if not destination.exists():
            staging.rename(destination)
        return subject
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_capture_set(store: LedgerStore, sha256: str) -> dict:
    """The cases of one stored set, in the shape `store_capture_set` took."""
    directory = capture_set_dir(store, sha256)
    if not directory.is_dir():
        raise FileNotFoundError(
            f"region {store.region_dir} holds no capture set {sha256}; a claim naming "
            f"it was filed against a ledger that no longer has it"
        )
    return npy.load_dataset(directory)


def program_variable(path: str) -> str:
    """The variable a file a program wrote is stored under: its path without the suffix.

    A file in a directory of the program's own keeps that directory in
    its name, so two files called `rho.npy` in different directories stay
    two variables.
    """
    return path[: -len(npy.INPUT_SUFFIX)] if path.endswith(npy.INPUT_SUFFIX) else path


def program_arrays(written: dict, declared) -> tuple[dict, dict]:
    """The declared files one run wrote, as arrays, and a message per file that is not one.

    `written` is what the builder hands back for one run: {path: base64 of
    that file's bytes}. The program's outputs are compared as arrays, like
    the region's, so a file that is not an NPY file is a problem named
    here -- keyed by the path it came from, so a caller comparing one
    output at a time can say which one -- rather than a comparison that
    quietly did not happen.
    """
    arrays = {}
    problems = {}
    for path in declared:
        try:
            arrays[program_variable(path)] = npy.decode(base64.b64decode(written[path]))
        except Exception as exc:
            problems[path] = (
                f"the timing run's '{path}' does not read as an array ({exc}); the "
                f"program's outputs are compared as arrays, like the region's"
            )
    return arrays, problems


def store_program_set(store: LedgerStore, arrays: dict) -> Subject:
    """Store what one timing run wrote as the set a later run is compared against."""
    return store_capture_set(
        store, PROGRAM_SET, {PROGRAM_SET: {"inputs": {}, "outputs": arrays}},
    )
