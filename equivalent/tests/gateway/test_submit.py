from pathlib import Path

from equivalent.gateway.submit import submit
from equivalent.ledger.acceptance import ONBOARDING
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import frozen_subject
from equivalent.region.current import current_commit, resolve_allow_globs
from equivalent.strategy.schema import load_strategy
from equivalent.tests.gateway.conftest import ONBOARDING_STRATEGY_PATH, STRATEGY_PATH
from equivalent.tree import Tree, init_baseline_repo

STRATEGY = load_strategy(STRATEGY_PATH)
ONBOARDING_STRATEGY = load_strategy(ONBOARDING_STRATEGY_PATH)


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


# A file that is not text at all -- a real code's tree holds small
# reference data next to its source.
BINARY_BYTES = bytes(range(256)) * 4


def test_file_outside_allow_list_is_rejected(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(working, "Makefile", "all:\n\techo changed\n")

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert {"path": "Makefile", "reason": "not_allowed"} in receipt.rejected
    tree = Tree(repo_dir, "region/ch04-step").files
    assert tree["Makefile"] == b"all:\n\techo build\n"


def test_a_file_the_strategy_allows_is_submitted_and_left_out_of_the_frozen_set(tmp_path):
    # Fortran spells the same extension both ways, so "src/*.f90" and
    # "src/Mod_Kernel.F90" have to be one file to everyone who reads the
    # allow-list. A file the strategy says a session may edit that submit
    # then turned away would be an edit nobody could file a claim about,
    # and hashing it into the frozen set would record it as held still
    # while it was being changed.
    seed = tmp_path / "seed"
    _write(seed, "src/Mod_Kernel.F90", "subroutine step\nend subroutine\n")
    _write(seed, "Makefile", "all:\n\techo build\n")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)

    working = tmp_path / "working"
    _write(working, "src/Mod_Kernel.F90", "subroutine step\n! ported\nend subroutine\n")

    assert STRATEGY.allows("src/Mod_Kernel.F90")
    receipt = submit(repo_dir, "ch04:step", working, list(STRATEGY.allow_globs), "sess-1")

    assert receipt.rejected == ()
    tree = Tree(repo_dir, "region/ch04-step").files
    assert tree["src/Mod_Kernel.F90"] == b"subroutine step\n! ported\nend subroutine\n"
    assert receipt.frozen == frozen_subject(
        [{"path": "Makefile", "content": b"all:\n\techo build\n"}]
    ).sha256


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

    tree = Tree(repo_dir, "region/ch04-step").files
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
    tree = Tree(repo_dir, "region/ch04-step").files
    assert tree["src/mod_stencil.f90"] == b"module mod_stencil\nend module\n"


def test_a_file_that_is_not_text_is_no_longer_a_rejection_reason(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", BINARY_BYTES)

    receipt = submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")

    assert receipt.rejected == ()
    tree = Tree(repo_dir, "region/ch04-step").files
    assert tree["src/mod_kernel.f90"] == BINARY_BYTES


def test_resolved_commit_remains_the_same_snapshot_after_a_later_submit(tmp_path):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))
    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 1\nend subroutine\n")
    submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-1")
    snapshot = current_commit(repo_dir, "ch04:step")

    _write(working, "src/mod_kernel.f90", "subroutine step\n  x = 2\nend subroutine\n")
    submit(repo_dir, "ch04:step", working, ["src/*.f90"], "sess-2")

    old = Tree(repo_dir, snapshot).files
    current = Tree(repo_dir, current_commit(repo_dir, "ch04:step")).files
    assert b"x = 1" in old["src/mod_kernel.f90"]
    assert b"x = 2" in current["src/mod_kernel.f90"]


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

    tree_paths = set(Tree(repo_dir, "region/ch04-step").files)
    assert tree_paths == {"src/mod_kernel.f90", "Makefile"}


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
    baseline_tree = Tree.baseline(repo_dir).sha
    assert receipt.tree == baseline_tree


def test_an_onboarding_region_may_submit_anything_the_strategy_allows(tmp_path):
    # There is no region and no analyzer claim while a code is being
    # brought in: the strategy's own allow-list is the whole answer, and
    # onboarding.yaml allows the tree.
    store = LedgerStore(tmp_path / "ledger")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))
    working = tmp_path / "working"
    _write(working, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(working, "harness/manifest.yaml", "version: 1\n")
    _write(working, "Makefile", "replay:\n\techo build\n")

    allow_globs = resolve_allow_globs(store, None, ONBOARDING, ONBOARDING_STRATEGY)
    receipt = submit(repo_dir, "tsunami:onboarding", working, allow_globs, "sess-1")

    assert allow_globs == ["*"]
    assert receipt.rejected == ()
    assert receipt.committed is True
