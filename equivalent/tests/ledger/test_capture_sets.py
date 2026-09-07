"""Storing a set of captured cases in a region's ledger, and reading it back.

These read as the statement of what a capture set is: a directory of
cases on disk, named by a hash of its own bytes, so that the same cases
stored twice are one artifact and one subject -- which is how a later
check can say "the second capture is the set I already have" without
comparing arrays.
"""
from __future__ import annotations

import numpy as np

from equivalent.capture import npy
from equivalent.ledger import capture_sets
from equivalent.ledger.store import LedgerStore


def _arrays(offset: int = 0) -> dict:
    return {
        "h": np.asarray([1, 2, 3], dtype="<f4") + offset,
        "u": np.asarray([[4, 5], [6, 7]], dtype="<f8") + offset,
    }


def _cases(offset: int = 0) -> dict:
    return {
        "case0000": {"inputs": _arrays(offset), "outputs": _arrays(offset + 1)},
        "case0001": {"inputs": _arrays(offset + 2), "outputs": _arrays(offset + 3)},
    }


def _store(tmp_path) -> LedgerStore:
    return LedgerStore(tmp_path / "region")


def test_a_stored_set_comes_back_case_for_case_and_array_for_array(tmp_path):
    store = _store(tmp_path)

    packed = capture_sets.pack_capture_set("visible", _cases())
    store.keep(packed)
    back = capture_sets.load_capture_set(store, packed.sha256)

    assert sorted(back) == ["case0000", "case0001"]
    assert np.array_equal(back["case0000"]["inputs"]["h"], _arrays()["h"])
    assert np.array_equal(back["case0001"]["outputs"]["u"], _arrays(3)["u"])
    assert back["case0000"]["outputs"]["h"].dtype == np.dtype("<f4")


def test_the_same_cases_stored_twice_are_one_subject_and_one_directory(tmp_path):
    store = _store(tmp_path)

    first = capture_sets.pack_capture_set("visible", _cases())
    second = capture_sets.pack_capture_set("holdout", _cases())
    store.keep(first)
    store.keep(second)

    assert first.sha256 == second.sha256
    directories = sorted(p.name for p in store.capture_sets_dir.iterdir())
    assert directories == [first.sha256]


def test_one_changed_element_makes_a_different_subject(tmp_path):
    changed = _cases()
    changed["case0001"]["outputs"]["h"] = _arrays(3)["h"] + 1

    first = capture_sets.pack_capture_set("visible", _cases())
    second = capture_sets.pack_capture_set("visible", changed)

    assert first.sha256 != second.sha256


def test_the_set_on_disk_is_the_layout_the_capture_reader_already_reads(tmp_path):
    # A person looking at a ledger sees case directories they can open,
    # not an archive they have to unpack first.
    store = _store(tmp_path)

    directory = store.keep(capture_sets.pack_capture_set("visible", _cases()))
    assert npy.dataset_cases(directory) == ["case0000", "case0001"]
    assert (directory / "case0000" / "h.npy").is_file()
    assert (directory / "case0000" / "h.out.npy").is_file()
    assert npy.read_case_names(directory / "case0000") == {
        "inputs": ["h", "u"], "outputs": ["h", "u"],
    }


def test_asking_for_a_set_the_region_never_stored_says_which_one(tmp_path):
    store = _store(tmp_path)

    try:
        capture_sets.load_capture_set(store, "0" * 64)
    except FileNotFoundError as caught:
        assert "0" * 64 in str(caught)
    else:
        raise AssertionError("a missing capture set must be reported, not answered")


def test_a_set_nobody_keeps_leaves_nothing_behind(tmp_path):
    # A check packs what it made and hands it back; only a verdict the
    # gateway is about to record makes it a set the ledger holds. What was
    # packed and not kept goes away with the value that owned it.
    import gc

    store = _store(tmp_path)
    packed = capture_sets.pack_capture_set("visible", _cases())
    directory = packed.directory

    assert directory.is_dir()
    del packed
    gc.collect()

    assert not directory.exists()
    assert list(store.capture_sets_dir.iterdir()) == []
