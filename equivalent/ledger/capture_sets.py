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

import json
import shutil
import tempfile
import weakref
from pathlib import Path

from equivalent.capture import npy
from equivalent.ledger.packed import PackedSet
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import hash_files
from equivalent.ledger.vocabulary import CAPTURE_SET_KEY

# What a timing run's own outputs are stored as: one dataset holding one
# case, whose variables are the files the program wrote. The baseline
# stores such a set and a port is compared against it, so both sides name
# it the same way here rather than each spelling it for itself.
PROGRAM_SET = "program"

# The claim that says which stored set each declared dataset was written
# into. It is spelled beside the sets themselves because two layers read
# it: the check that files it, and the promotion command that copies the
# sets it named into the code's own directory.
CAPTURED_PREDICATE = "harness/captured"


def sets_named_by(claim, where: str) -> dict:
    """The capture set each dataset was stored under, from one capture claim.

    This is how anyone finds the arrays a passing capture approved: by
    reading the claim, never by capturing again. A passing claim that
    names no set at all is broken evidence rather than a verdict about
    the code, so it is raised; a caller decides whether that is an error
    on the harness's side or a reason to refuse.
    """
    sets = {
        name: entry[CAPTURE_SET_KEY]
        for name, entry in claim.predicate.detail.get("datasets", {}).items()
        if entry.get(CAPTURE_SET_KEY)
    }
    if not sets:
        raise ValueError(f"the {CAPTURED_PREDICATE} claim for {where} names no capture set")
    return sets


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
    `name` is what the manifest calls this dataset. It names the staging
    directory, so a person looking at what a run left behind can see
    which dataset it was, and it is kept out of the hash, so that two
    datasets holding the same arrays are one artifact rather than two
    copies that a later comparison would have to know are the same.

    Nothing is filed here. The directory belongs to the returned value and
    goes away with it unless a store keeps it, so a check can pack a set,
    compare it with one already stored, and hand back only what should
    survive.
    """
    readable = "".join(c if c.isalnum() or c in "._-" else "-" for c in name)
    staging = Path(tempfile.mkdtemp(prefix=f"equivalent-set-{readable}-"))
    write_dataset(staging, cases)
    packed = PackedSet(sha256=hash_files(_files_under(staging)), directory=staging)
    weakref.finalize(packed, shutil.rmtree, staging, True)
    return packed


def load_capture_set(store: LedgerStore, sha256: str) -> dict:
    """The cases of one stored set, in the shape `pack_capture_set` took."""
    return SetReader(store.capture_sets_dir).load(sha256)


def pack_program_set(arrays: dict) -> PackedSet:
    """What one timing run wrote, as the set a later run is compared against."""
    return pack_capture_set(
        PROGRAM_SET, {PROGRAM_SET: {"inputs": {}, "outputs": arrays}},
    )
