import base64
from pathlib import Path

import pytest

from equivalent.gateway.submit import (
    init_baseline_repo,
    materialize_tree,
    resolve_allow_globs,
    submit,
    tracked_files,
    tree_payload,
)
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import tree_subject


def _write(root, path, content):
    if isinstance(content, bytes):
        p = Path(root) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    else:
        p = Path(root) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _seed(root):
    _write(root, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(root, "Makefile", "all:\n\techo build\n")
    return root


# The hash of that two-file baseline, taken while submit still read every
# tracked file as UTF-8 text. Carrying bytes instead must not move it: a
# tree hash that changed would orphan every claim already in a ledger.
TEXT_BASELINE_TREE = "340f09cc8926f612b1e671ce336fce9a319f74acae80d523c87d67463bedea75"

# A file in an encoding that is not UTF-8, and one that is not text at all.
# A real code's tree has both -- namelists written on another machine, small
# reference data next to the source.
LATIN1_BYTES = "! coefficient d'entr\u00e9e\n".encode("latin-1")
BINARY_BYTES = bytes(range(256)) * 4


def test_init_baseline_repo_matches_seed_folder(tmp_path):
    seed = _seed(tmp_path / "seed")
    repo_dir = tmp_path / "repo"

    baseline_commit = init_baseline_repo(repo_dir, seed)

    assert len(baseline_commit) == 40
    files = {f["path"]: f["content"] for f in tracked_files(repo_dir)}
    assert files == {
        "src/mod_kernel.f90": b"subroutine step\nend subroutine\n",
        "Makefile": b"all:\n\techo build\n",
    }


def test_file_outside_allow_list_is_rejected(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(working, "Makefile", "all:\n\techo changed\n")

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert {"path": "Makefile", "reason": "not_allowed"} in receipt.rejected
    tree = {f["path"]: f["content"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree["Makefile"] == b"all:\n\techo build\n"


def test_new_allowed_file_is_added_new_disallowed_file_is_rejected(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(working, "notes/regions/ch04-step.sese.yaml", "region: ch04:step\n")
    _write(working, "scripts/helper.sh", "echo hi\n")

    receipt = submit(
        repo_dir, "ch04:step", working, ["src/*.f90", "notes/regions/*.yaml"], "sess-1",
    )

    tree = {f["path"]: f["content"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree["notes/regions/ch04-step.sese.yaml"] == b"region: ch04:step\n"
    assert "scripts/helper.sh" not in tree
    assert {"path": "scripts/helper.sh", "reason": "not_allowed"} in receipt.rejected


def test_a_file_the_region_creates_is_committed_and_is_not_a_missing_file(tmp_path):
    # A region may list a file that does not exist in the baseline yet --
    # a port that splits a stencil into its own module writes one. It is
    # committed like any other allowed file, and `not_sent` must not warn
    # about it: there is no baseline copy for a stale one to ride along.
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(working, "src/mod_stencil.f90", "module mod_stencil\nend module\n")

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert receipt.committed is True
    assert receipt.rejected == ()
    assert receipt.not_sent == ()
    tree = {f["path"]: f["content"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree["src/mod_stencil.f90"] == b"module mod_stencil\nend module\n"


def test_bytes_that_are_not_utf8_survive_seed_repo_submit_and_materialize(tmp_path):
    # A code's tree is not all UTF-8 source: it holds namelists in other
    # encodings and small data files. Whatever the baseline holds has to
    # come back out of the gateway's repository byte for byte, or a claim
    # is about a tree that is not the one the person is reading.
    seed = _seed(tmp_path / "seed")
    _write(seed, "data/coeffs.nml", LATIN1_BYTES)
    _write(seed, "data/table.bin", BINARY_BYTES)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 1\nend subroutine\n")
    _write(working, "src/table.f90", BINARY_BYTES)

    submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    tree = {f["path"]: f["content"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree["data/coeffs.nml"] == LATIN1_BYTES
    assert tree["data/table.bin"] == BINARY_BYTES
    assert tree["src/table.f90"] == BINARY_BYTES

    out = tmp_path / "materialized"
    materialize_tree(repo_dir, "region/ch04-step", out)
    assert (out / "data" / "coeffs.nml").read_bytes() == LATIN1_BYTES
    assert (out / "data" / "table.bin").read_bytes() == BINARY_BYTES
    assert (out / "src" / "table.f90").read_bytes() == BINARY_BYTES


def test_a_file_that_is_not_text_is_no_longer_a_rejection_reason(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", BINARY_BYTES)

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert receipt.rejected == ()
    tree = {f["path"]: f["content"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree["src/mod_kernel.f90"] == BINARY_BYTES


def test_an_all_text_baseline_hashes_exactly_as_it_did_before(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    assert tree_subject(tracked_files(repo_dir, "main")).sha256 == TEXT_BASELINE_TREE


def test_the_whole_tree_is_handed_to_the_builder_as_bytes(tmp_path):
    # The builder builds the tree with the tree's own makefile, which may
    # read a namelist or a data file no extension test would recognize --
    # so everything tracked goes, base64 because the request is JSON and
    # a real code's tree is not all UTF-8.
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    files = tree_payload(repo_dir, "main")

    assert [f["path"] for f in files] == ["Makefile", "src/mod_kernel.f90"]
    assert base64.b64decode(files[1]["b64"]) == b"subroutine step\nend subroutine\n"


def test_a_file_the_manifest_does_not_call_source_is_sent_anyway(tmp_path):
    # Which files the code calls source decides what the builder may
    # compile, not what it is given: a README costs nothing to carry, and
    # guessing wrong about a build input costs a build.
    seed = _seed(tmp_path / "seed")
    _write(seed, "README.md", "how to build this\n")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)

    files = tree_payload(repo_dir, "main")

    assert "README.md" in [f["path"] for f in files]


def test_a_file_that_is_not_utf8_travels_unchanged(tmp_path):
    seed = _seed(tmp_path / "seed")
    _write(seed, "src/legacy.f90", LATIN1_BYTES)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)

    files = {f["path"]: base64.b64decode(f["b64"]) for f in tree_payload(repo_dir, "main")}

    assert files["src/legacy.f90"] == LATIN1_BYTES


def test_submitting_the_same_contents_twice_creates_no_second_commit(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 1\nend subroutine\n")

    first = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")
    second = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert first.tree == second.tree
    assert first.committed is True
    assert second.committed is False


def test_frozen_hash_unaffected_by_allowed_edit_but_changed_by_baseline_edit(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 1\nend subroutine\n")
    before = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 2\nend subroutine\n")
    after_allowed_edit = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")
    assert after_allowed_edit.frozen == before.frozen

    # A second gateway repo standing in for "the baseline changed": same
    # region, same allowed file, a different Makefile outside the allow-list.
    repo_dir_changed = tmp_path / "repo-changed"
    seed_changed = tmp_path / "seed-changed"
    _write(seed_changed, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(seed_changed, "Makefile", "all:\n\techo CHANGED\n")
    init_baseline_repo(repo_dir_changed, seed_changed)

    after_baseline_change = submit(repo_dir_changed, "ch04:step", working, ["src/*.f90"], "sess-1")
    assert after_baseline_change.frozen != before.frozen


def test_constructed_tree_never_contains_a_disallowed_file(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 1\nend subroutine\n")
    _write(working, "scripts/helper.sh", "echo hi\n")

    submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    tree_paths = {f["path"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree_paths == {"src/mod_kernel.f90", "Makefile"}


def test_resolve_allow_globs_before_and_after_sese_verified(tmp_path):
    store = LedgerStore(tmp_path / "region")
    spec_path = "notes/regions/ch04-step.sese.yaml"

    assert resolve_allow_globs(store, spec_path) == [spec_path]

    store.record_claim(
        [], "sese/verified",
        Predicate(
            tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
            detail={"allow_globs": ["src/mod_kernel.f90", spec_path]},
        ),
        [], "sess-1",
    )

    assert resolve_allow_globs(store, spec_path) == ["src/mod_kernel.f90", spec_path]


def test_receipt_names_allowed_baseline_paths_that_were_not_sent(tmp_path):
    # An allowed file the agent forgot to send is named in the receipt,
    # so a stale baseline copy riding along silently is visible.
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    working.mkdir()
    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")
    assert receipt.not_sent == ("src/mod_kernel.f90",)

    _write(working, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")
    assert receipt.not_sent == ()


def test_submit_with_nothing_matching_allow_list_is_a_no_op(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "README.md", "not part of the region\n")

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert receipt.committed is False
    baseline_tree = tree_subject(tracked_files(repo_dir, "main")).sha256
    assert receipt.tree == baseline_tree
