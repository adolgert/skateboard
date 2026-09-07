from pathlib import Path

from equivalent.gateway.submit import (
    current_commit,
    init_baseline_repo,
    resolve_allow_globs,
    submit,
)
from equivalent.ledger.acceptance import ONBOARDING, PORTING
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.strategy.schema import load_strategy
from equivalent.tree import Tree

STRATEGY = load_strategy(
    Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
)
ONBOARDING_STRATEGY = load_strategy(
    Path(__file__).resolve().parents[2] / "strategy" / "files" / "onboarding.yaml"
)


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
    files = Tree.baseline(repo_dir).files
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
    tree = Tree(repo_dir, "region/ch04-step").files
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

    tree = Tree(repo_dir, "region/ch04-step").files
    assert tree["data/coeffs.nml"] == LATIN1_BYTES
    assert tree["data/table.bin"] == BINARY_BYTES
    assert tree["src/table.f90"] == BINARY_BYTES

    out = tmp_path / "materialized"
    Tree(repo_dir, "region/ch04-step").write_to(out)
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


def test_resolve_allow_globs_before_and_after_sese_verified(tmp_path):
    store = LedgerStore(tmp_path / "region")
    spec_path = "notes/regions/ch04-step.sese.yaml"

    assert resolve_allow_globs(store, spec_path, PORTING, STRATEGY) == [spec_path]

    store.record_claim(
        [], "sese/verified",
        Predicate(
            tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
            detail={"allow_globs": ["src/mod_kernel.f90", spec_path]},
        ),
        [], "sess-1",
    )

    assert resolve_allow_globs(store, spec_path, PORTING, STRATEGY) == ["src/mod_kernel.f90", spec_path]


def test_resolve_allow_globs_does_not_trust_paths_outside_reviewed_strategy(tmp_path):
    store = LedgerStore(tmp_path / "region")
    spec_path = "notes/regions/ch04-step.sese.yaml"
    store.record_claim(
        [], "sese/verified",
        Predicate(
            tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
            detail={"allow_globs": ["*", spec_path]},
        ),
        [], "sess-1",
    )

    assert resolve_allow_globs(store, spec_path, PORTING, STRATEGY) == [spec_path]


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


def test_an_onboarding_regions_frozen_set_is_whatever_its_allow_list_leaves(tmp_path):
    # With the whole tree allowed, nothing is frozen -- and an empty
    # frozen set is a real value, not a missing one.
    from equivalent.gateway.submit import current_tree_and_frozen
    from equivalent.ledger.subjects import frozen_subject

    store = LedgerStore(tmp_path / "ledger")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))

    _, frozen_sha = current_tree_and_frozen(
        repo_dir, "tsunami:onboarding", store, None, ONBOARDING, ONBOARDING_STRATEGY,
    )

    assert frozen_sha == frozen_subject([]).sha256
