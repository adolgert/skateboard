"""Typed executable artifacts and the known build-claim detail codecs.

Claim detail is a durable, human-readable wire format.  It is deliberately
not an artifact discovery API: an incidental ``sha256`` or ``targets`` key in
diagnostic detail must never become evidence.  Fresh checks declare artifacts
as these records, while old build claims are decoded only through the two
layouts their predicates are specified to write.
"""
from __future__ import annotations

from dataclasses import dataclass

from equivalent.ledger.subjects import Subject, is_digest


@dataclass(frozen=True)
class BinaryArtifact:
    """One executable a backend says it measured."""

    executable: str
    sha256: str
    size: int | None = None

    def __post_init__(self):
        if not isinstance(self.executable, str) or not self.executable:
            raise ValueError("binary artifact executable must be a non-empty string")
        if not is_digest(self.sha256):
            raise ValueError("binary artifact sha256 must be a lowercase SHA-256 digest")
        if self.size is not None and (
            isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0
        ):
            raise ValueError("binary artifact size must be a non-negative integer")

    @property
    def subject(self) -> Subject:
        return Subject(kind="binary", sha256=self.sha256)


@dataclass(frozen=True)
class BuildTarget:
    """One role and the executable bytes a successful build put behind it."""

    role: str
    executable: str
    sha256: str
    size: int | None = None

    def __post_init__(self):
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("build target role must be a non-empty string")
        BinaryArtifact(self.executable, self.sha256, self.size)

    @property
    def artifact(self) -> BinaryArtifact:
        return BinaryArtifact(self.executable, self.sha256, self.size)

    def as_detail(self) -> dict:
        """The identity fields used to compare with a builder artifacts reply."""
        detail = {"executable": self.executable, "sha256": self.sha256}
        if self.size is not None:
            detail["size"] = self.size
        return detail


@dataclass(frozen=True)
class BuildRecord:
    """The executables produced in one builder workspace."""

    attempt_id: str
    targets: tuple[BuildTarget, ...]

    def __post_init__(self):
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("build attempt_id must be a non-empty string")

    @property
    def binary_artifacts(self) -> tuple[BinaryArtifact, ...]:
        return tuple(target.artifact for target in self.targets)


def binary_artifact(identity, *, executable: str | None = None) -> BinaryArtifact | None:
    """Decode a backend executable identity, or no declaration for ``None``.

    This reads exactly the response field's own layout.  It does not walk a
    response or claim looking for similarly named nested objects.
    """
    if identity is None:
        return None
    if not isinstance(identity, dict):
        raise ValueError("executable identity must be an object")
    return BinaryArtifact(
        executable=identity.get("executable", executable),
        sha256=identity.get("sha256"),
        size=identity.get("size"),
    )


def binary_artifacts(
    *identities, executable: str | None = None,
) -> tuple[BinaryArtifact, ...]:
    """Decode, de-duplicate, and retain explicitly supplied identities."""
    found = {}
    for identity in identities:
        artifact = binary_artifact(identity, executable=executable)
        if artifact is not None:
            found[(artifact.executable, artifact.sha256, artifact.size)] = artifact
    return tuple(found.values())


def build_record(attempt_id, targets) -> BuildRecord:
    """Decode the target table in one freshly typed build response."""
    if not isinstance(targets, dict):
        raise ValueError("build targets must be an object")
    if any(not isinstance(role, str) or not role for role in targets):
        raise ValueError("build target roles must be non-empty strings")
    decoded = []
    for role, target in sorted(targets.items()):
        if not isinstance(target, dict):
            raise ValueError(f"build target {role!r} must be an object")
        decoded.append(BuildTarget(
            role=role,
            executable=target.get("executable"),
            sha256=target.get("sha256"),
            size=target.get("size"),
        ))
    return BuildRecord(attempt_id=attempt_id, targets=tuple(decoded))


def decode_build_records(predicate_type: str, detail) -> tuple[BuildRecord, ...]:
    """Decode a stored build claim through its predicate's specified layout.

    Malformed history answers with no records.  Callers treat that as missing
    build evidence, which is the fail-closed outcome for status and restore.
    """
    if not isinstance(detail, dict):
        return ()
    try:
        if predicate_type == "build/replay":
            return (build_record(detail.get("attempt_id"), detail.get("targets")),)
        if predicate_type == "harness/builds":
            strategies = detail.get("strategies")
            if not isinstance(strategies, dict):
                return ()
            if any(not isinstance(entry, dict) for entry in strategies.values()):
                return ()
            return tuple(
                build_record(entry.get("attempt_id"), entry.get("targets"))
                for _, entry in sorted(strategies.items())
            )
    except (TypeError, ValueError):
        return ()
    return ()


def materials_for_artifacts(artifacts) -> tuple[Subject, ...]:
    """Binary subjects for explicit artifacts, in stable digest order."""
    return tuple(
        Subject(kind="binary", sha256=digest)
        for digest in sorted({artifact.sha256 for artifact in artifacts})
    )


def build_binary_materials(predicate_type: str, detail) -> tuple[Subject, ...]:
    """Binary cohort declared by one stored build claim."""
    return materials_for_artifacts(
        artifact
        for record in decode_build_records(predicate_type, detail)
        for artifact in record.binary_artifacts
    )
