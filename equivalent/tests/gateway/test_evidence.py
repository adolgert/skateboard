from dataclasses import replace

from equivalent.gateway.evidence import (
    NO_CURRENT_BUILD,
    evidence_materials_for,
    required_materials_by_predicate,
)
from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS, PORTING
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.tests.gateway.test_run import _region


def _by_kind(materials, kind):
    return [material.sha256 for material in materials if material.kind == kind]


def test_tolerance_and_visible_dataset_bytes_are_part_of_current_context(tmp_path):
    cfg = _region(tmp_path)
    visible = tmp_path / "visible"
    visible.mkdir()
    (visible / "case.bin").write_bytes(b"first")
    cfg = replace(cfg, visible_dataset_dir=visible)
    before = evidence_materials_for(cfg)

    cfg.manifest.tolerances.write_bytes(cfg.manifest.tolerances.read_bytes() + b"\n")
    (visible / "case.bin").write_bytes(b"second")
    after = evidence_materials_for(cfg)

    assert _by_kind(before, "policy") != _by_kind(after, "policy")
    assert _by_kind(before, "capture_set") != _by_kind(after, "capture_set")


def test_reviewed_backend_identity_pins_are_static_materials(tmp_path):
    cfg = replace(
        _region(tmp_path), executor_identity="a" * 64, oracle_identity="b" * 64,
    )

    materials = evidence_materials_for(cfg)

    assert _by_kind(materials, "executor") == ["a" * 64]
    assert _by_kind(materials, "oracle") == ["b" * 64]


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
