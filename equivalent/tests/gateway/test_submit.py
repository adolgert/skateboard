from pathlib import Path

from equivalent.gateway.submit import (
    init_baseline_repo,
    resolve_allow_globs,
    submit,
    tracked_files,
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


def test_init_baseline_repo_matches_seed_folder(tmp_path):
    seed = _seed(tmp_path / "seed")
    repo_dir = tmp_path / "repo"

    baseline_commit = init_baseline_repo(repo_dir, seed)

    assert len(baseline_commit) == 40
    files = {f["path"]: f["content"] for f in tracked_files(repo_dir)}
    assert files == {
        "src/mod_kernel.f90": "subroutine step\nend subroutine\n",
        "Makefile": "all:\n\techo build\n",
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
    assert tree["Makefile"] == "all:\n\techo build\n"


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
    assert tree["notes/regions/ch04-step.sese.yaml"] == "region: ch04:step\n"
    assert "scripts/helper.sh" not in tree
    assert {"path": "scripts/helper.sh", "reason": "not_allowed"} in receipt.rejected


def test_binary_file_at_an_allowed_path_is_rejected(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", b"\xff\xfe\x00\x01")

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert {"path": "src/mod_kernel.f90", "reason": "binary"} in receipt.rejected
    tree = {f["path"]: f["content"] for f in tracked_files(repo_dir, "region/ch04-step")}
    assert tree["src/mod_kernel.f90"] == "subroutine step\nend subroutine\n"


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


def test_submit_with_nothing_matching_allow_list_is_a_no_op(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "README.md", "not part of the region\n")

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert receipt.committed is False
    baseline_tree = tree_subject(tracked_files(repo_dir, "main")).sha256
    assert receipt.tree == baseline_tree
