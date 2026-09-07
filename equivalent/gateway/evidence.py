"""The current external inputs that make a claim valid for a region."""
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


BUILD_PREDICATE = {"porting": "build/replay", "onboarding": "harness/builds"}
FOUNDATION_PREDICATES = {
    "sese/verified", "manifest/valid", "build/replay", "harness/builds",
    "timing/baseline",
}
NO_CURRENT_BUILD = Subject(
    kind="binary", sha256=hash_bytes(b"equivalent:no-current-build:v1"),
)


def binary_materials(detail: dict) -> tuple[Subject, ...]:
    """Executable identities retained anywhere in structured claim detail."""
    digests = set()

    def visit(value, key=None):
        if isinstance(value, dict):
            if key == "executable_identity" and isinstance(value.get("sha256"), str):
                digests.add(value["sha256"])
            if key == "targets":
                for target in value.values() if isinstance(value, dict) else ():
                    if isinstance(target, dict) and isinstance(target.get("sha256"), str):
                        digests.add(target["sha256"])
            for child_key, child in value.items():
                visit(child, child_key)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(detail)
    return tuple(Subject(kind="binary", sha256=digest) for digest in sorted(digests))


def current_build_claim(store, phase: str, tree: Subject, core_materials=()):
    """The current passing build assertion for one candidate tree, if any."""
    claim = store.latest(
        BUILD_PREDICATE[phase], tree, required_materials=core_materials,
    )
    return claim if claim is not None and claim.predicate.verdict == "pass" else None


def required_materials_by_predicate(
    store, requirements, phase: str, tree: Subject, core_materials=(),
) -> dict[str, tuple[Subject, ...]]:
    """Material context for each dependent predicate in status/promotion.

    Foundation claims predate the executable cohort and continue to use the
    caller's core context. Every later claim must contain all identities from
    the current passing build. The sentinel makes dependent legacy evidence
    stale when no usable build cohort exists.
    """
    build_claim = current_build_claim(store, phase, tree, core_materials)
    cohort = binary_materials(build_claim.predicate.detail) if build_claim else ()
    dependent = (*core_materials, *(cohort or (NO_CURRENT_BUILD,)))
    return {
        requirement.predicate_type: dependent
        for requirement in requirements
        if requirement.predicate_type not in FOUNDATION_PREDICATES
    }


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
