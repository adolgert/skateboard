"""Capture sets kept in a region's ledger, named by a hash of their bytes.

A capture set is a dataset of cases: the inputs a run of the code's own
capture program dumped at the region's call site, and the outputs it
dumped one call later. It is stored as the same directory layout the
capture reader already reads -- one directory per case, one NPY file per
variable -- so a person reviewing a ledger opens files rather than
unpacking an archive.

A set is packed here and kept by the store: a check writes the arrays
into a directory of its own and hands back its hash, and only a verdict
the gateway is about to record moves that directory into the ledger. A
set that failed its own check is therefore never there for a later
comparison to find.

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
import weakref
from pathlib import Path

from equivalent.capture import npy
from equivalent.ledger.packed import PackedSet
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import hash_files

# The subject kind a stored set's hash names.
CAPTURE_SET = "capture_set"

# What a timing run's own outputs are stored as: one dataset holding one
# case, whose variables are the files the program wrote. The baseline
# stores such a set and a port is compared against it, so both sides name
# it the same way here rather than each spelling it for itself.
PROGRAM_SET = "program"


class SetReader:
    """Read-only access to the capture sets one region's ledger holds.

    A check is given one of these rather than the store, because reading
    the arrays a verdict is measured against is all a check has any
    business doing with the ledger: it cannot file a claim through this,
    and it cannot leave a set behind through it either.
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, sha256: str) -> Path:
        """Where one set's cases sit. It may not exist."""
        return self.directory / sha256

    def load(self, sha256: str) -> dict:
        """The cases of one stored set, in the shape `pack_capture_set` took."""
        directory = self.path(sha256)
        if not directory.is_dir():
            raise FileNotFoundError(
                f"{self.directory} holds no capture set {sha256}; a claim naming "
                f"it was filed against a ledger that no longer has it"
            )
        return npy.load_dataset(directory)


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


def pack_capture_set(name: str, cases: dict) -> PackedSet:
    """Write one dataset of cases somewhere of its own and name it by its content.

    `cases` is {case: {"inputs": {variable: array}, "outputs": {...}}},
    which is what the capture reader hands back for a dataset directory.
    `name` is what the manifest calls this dataset; it is for the caller's
    own messages and is deliberately not part of the hash, so that two
    datasets holding the same arrays are one artifact rather than two
    copies that a later comparison would have to know are the same.

    Nothing is filed here. The directory belongs to the returned value and
    goes away with it unless a store keeps it, so a check can pack a set,
    compare it with one already stored, and hand back only what should
    survive.
    """
    staging = Path(tempfile.mkdtemp(prefix="equivalent-set-"))
    write_dataset(staging, cases)
    packed = PackedSet(
        kind=CAPTURE_SET, name=name,
        sha256=hash_files(_files_under(staging)), directory=staging,
    )
    weakref.finalize(packed, shutil.rmtree, staging, True)
    return packed


def load_capture_set(store: LedgerStore, sha256: str) -> dict:
    """The cases of one stored set, in the shape `pack_capture_set` took."""
    return SetReader(store.capture_sets_dir).load(sha256)


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


def pack_program_set(arrays: dict) -> PackedSet:
    """What one timing run wrote, as the set a later run is compared against."""
    return pack_capture_set(
        PROGRAM_SET, {PROGRAM_SET: {"inputs": {}, "outputs": arrays}},
    )
