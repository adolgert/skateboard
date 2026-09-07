"""Which build a later claim has to have been reached on top of."""
from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS, PORTING
from equivalent.ledger.evidence import NO_CURRENT_BUILD, required_materials_by_predicate
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject


def test_dependent_predicates_require_the_current_build_binary_cohort(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    tree = Subject(kind="tree", sha256="1" * 64)
    core = (Subject(kind="strategy", sha256="2" * 64),)
    store.record_claim(
        [tree], "build/replay",
        Predicate(
            tool="builder", version="0.1", configHash="cfg", verdict="pass",
            detail={
                "attempt_id": "attempt",
                "targets": {"replay": {
                    "executable": "replay", "sha256": "3" * 64, "size": 10,
                }},
            },
        ),
        core, "session",
    )

    contexts = required_materials_by_predicate(
        store, ACCEPTANCE_REQUIREMENTS, PORTING, tree, core,
    )

    assert "build/replay" not in contexts
    assert Subject(kind="binary", sha256="3" * 64) in contexts["gpu/executed"]
    assert contexts["sanitize/initcheck"] == contexts["gpu/executed"]


def test_missing_build_uses_a_sentinel_that_no_legacy_claim_can_satisfy(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    tree = Subject(kind="tree", sha256="1" * 64)

    contexts = required_materials_by_predicate(
        store, ACCEPTANCE_REQUIREMENTS, PORTING, tree, (),
    )

    assert NO_CURRENT_BUILD in contexts["gpu/executed"]
