"""The leaf record returned by every check.

It imports no component policy, so contexts and phase policy can both depend
on it without a lazy runtime import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from equivalent.ledger.artifacts import BinaryArtifact, BuildRecord, materials_for_artifacts
from equivalent.ledger.vocabulary import FAIL


@dataclass(frozen=True)
class CheckResult:
    """A verdict and the complete declarations the gateway needs to file it."""

    verdict: str
    detail: dict
    reasons: tuple = ()
    materials: tuple = ()
    stores: tuple = ()
    subject_kind: str = "tree"
    measures_other_binaries: bool = False
    # Fresh results declare executable evidence here. Detail remains the
    # stable serialized explanation and is never searched for artifacts.
    binary_artifacts: tuple[BinaryArtifact, ...] = ()
    # Build checks additionally declare the workspace-to-target relationship
    # needed to reverify and restore the builder's protected artifacts.
    build_records: tuple[BuildRecord, ...] = ()

    @property
    def binary_materials(self) -> tuple:
        return materials_for_artifacts((
            *self.binary_artifacts,
            *(artifact for record in self.build_records
              for artifact in record.binary_artifacts),
        ))


def failed(detail: dict, reasons, **declarations) -> CheckResult:
    """A fail whose detail already holds the same words the session reads."""
    return CheckResult(
        verdict=FAIL, detail=detail, reasons=tuple(reasons), **declarations,
    )
