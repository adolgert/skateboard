"""What a region is right now: which files may change, and what is held still.

The allow-list is the reviewed ceiling on a port, and the frozen-set hash
is what makes an edit outside it visible afterwards. Both are read here
straight from a ledger and a repository, without a gateway, because the
CLI reads them the same way.
"""
from pathlib import Path

from equivalent.ledger.acceptance import ONBOARDING, PORTING
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import frozen_subject
from equivalent.region.current import current_tree_and_frozen, resolve_allow_globs
from equivalent.strategy.schema import load_strategy
from equivalent.tests.gateway.conftest import ONBOARDING_STRATEGY_PATH, STRATEGY_PATH
from equivalent.tree import init_baseline_repo

STRATEGY = load_strategy(STRATEGY_PATH)
ONBOARDING_STRATEGY = load_strategy(ONBOARDING_STRATEGY_PATH)
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"


def _seed(root):
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    (root / "Makefile").write_text("all:\n\techo build\n")
    return root


def _sese_pass(store, allow_globs):
    store.record_claim(
        [], "sese/verified",
        Predicate(
            tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
            detail={"allow_globs": allow_globs},
        ),
        [], "sess-1",
    )


def test_resolve_allow_globs_before_and_after_sese_verified(tmp_path):
    store = LedgerStore(tmp_path / "region")

    assert resolve_allow_globs(store, SPEC_PATH, PORTING, STRATEGY) == [SPEC_PATH]

    _sese_pass(store, ["src/mod_kernel.f90", SPEC_PATH])

    assert resolve_allow_globs(store, SPEC_PATH, PORTING, STRATEGY) == [
        "src/mod_kernel.f90", SPEC_PATH,
    ]


def test_resolve_allow_globs_does_not_trust_paths_outside_reviewed_strategy(tmp_path):
    store = LedgerStore(tmp_path / "region")
    _sese_pass(store, ["*", SPEC_PATH])

    assert resolve_allow_globs(store, SPEC_PATH, PORTING, STRATEGY) == [SPEC_PATH]


def test_an_onboarding_regions_frozen_set_is_whatever_its_allow_list_leaves(tmp_path):
    # With the whole tree allowed, nothing is frozen -- and an empty
    # frozen set is a real value, not a missing one.
    store = LedgerStore(tmp_path / "ledger")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(Path(tmp_path / "seed")))

    _, frozen_sha = current_tree_and_frozen(
        repo_dir, "tsunami:onboarding", store, None, ONBOARDING, ONBOARDING_STRATEGY,
    )

    assert frozen_sha == frozen_subject([]).sha256
