"""The files outside the tree that a region's claims are reached under.

Nothing here needs a repository or a gateway: the materials are read off
disk from the region's own configuration, which is the point -- a reader
on the host recomputes the same context the gateway did.
"""
from dataclasses import replace
from pathlib import Path

from equivalent.ledger.acceptance import PORTING
from equivalent.ledger.subjects import policy_subject
from equivalent.manifest.schema import load_manifest
from equivalent.region.config import RegionConfig
from equivalent.region.evidence import evidence_materials_for
from equivalent.tests.fakes import write_program

SPEC_PATH = "notes/regions/ch04-step.sese.yaml"
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
BASELINE_STRATEGY_PATH = STRATEGY_PATH.parent / "cpu_reference.yaml"


def _region(tmp_path) -> RegionConfig:
    return RegionConfig(
        region_id="ch04:step", code="tsunami", phase=PORTING,
        repo_dir=tmp_path / "repo",
        spec_path=SPEC_PATH,
        ledger_dir=tmp_path / "ledger",
        strategy_path=STRATEGY_PATH, baseline_strategy_path=BASELINE_STRATEGY_PATH,
        working_copy_dir=tmp_path / "working",
        manifest=load_manifest(write_program(tmp_path) / "manifest.yaml"),
    )


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


def test_the_tolerance_policy_is_the_subject_the_oracle_names_it_by(tmp_path):
    # The oracle hashes the policy bytes it was handed and returns that
    # digest in the answer a check turns into a claim. If the material
    # every claim carries were computed some other way, one file would
    # arrive on a claim as two different policy subjects, and a pass
    # under the current policy would read as reached under another.
    cfg = _region(tmp_path)

    materials = evidence_materials_for(cfg)

    assert policy_subject(cfg.manifest.tolerances.read_bytes()) in materials
