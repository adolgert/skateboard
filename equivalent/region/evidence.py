"""The current external inputs that make a claim valid for a region.

Trust role: a claim is only about the code as it stands if the files
that decided the answer are still the files on disk. The strategies the
build uses, the manifest, the tolerance and property policies it names,
the visible dataset, the pre-onboarding reference, and the pinned
backend identities are all outside the submitted tree, so none of them
is in the tree hash. Hashing them into every claim's materials is what
makes an edit to any of them retire the evidence that was reached under
the old one, instead of leaving it standing and unremarked.

Everything here is recomputed from what is on disk right now. Nothing is
cached: a stale copy would be a claim vouching for a configuration
nobody is running.
"""
from __future__ import annotations

from pathlib import Path

from equivalent.ledger.subjects import (
    Subject,
    evidence_policy_subject,
    hash_bytes,
    hash_files,
)
from equivalent.reference.schema import fingerprint_reference
from equivalent.strategy.schema import load_strategy


def _files_subject(path: Path, kind: str) -> Subject:
    path = Path(path)
    if path.is_file():
        digest = hash_files([{"path": path.name, "content": path.read_bytes()}])
    elif path.is_dir():
        digest = hash_files([
            {"path": p.relative_to(path).as_posix(), "content": p.read_bytes()}
            for p in sorted(path.rglob("*")) if p.is_file()
        ])
    else:
        digest = hash_bytes(f"absent:{path}".encode("utf-8"))
    return Subject(kind=kind, sha256=digest)


def evidence_materials_for(cfg, strategy=None, baseline_strategy=None) -> tuple[Subject, ...]:
    """Recompute the dependencies shared by every current region claim."""
    strategy = strategy or load_strategy(cfg.strategy_path)
    baseline_strategy = baseline_strategy or load_strategy(cfg.baseline_strategy_path)
    materials = [
        evidence_policy_subject(),
        strategy.as_subject(),
        baseline_strategy.as_subject(),
        cfg.manifest.as_subject(),
    ]
    if cfg.manifest.tolerances is not None:
        materials.append(_files_subject(cfg.manifest.tolerances, "policy"))
    if cfg.manifest.properties is not None:
        materials.append(_files_subject(cfg.manifest.properties, "policy"))
    if cfg.visible_dataset_dir is not None:
        materials.append(_files_subject(cfg.visible_dataset_dir, "capture_set"))
    if cfg.original_reference_path is not None:
        materials.append(Subject(
            kind="reference", sha256=fingerprint_reference(cfg.original_reference_path),
        ))
    else:
        materials.append(Subject(
            kind="reference", sha256=hash_bytes(b"equivalent:no-original-reference:v1"),
        ))
    executor_pin = getattr(cfg, "executor_identity", None)
    oracle_pin = getattr(cfg, "oracle_identity", None)
    if executor_pin is not None:
        materials.append(Subject(kind="executor", sha256=executor_pin))
    if oracle_pin is not None:
        materials.append(Subject(kind="oracle", sha256=oracle_pin))
    return tuple(materials)
