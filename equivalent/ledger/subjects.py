"""What a claim or artifact is about.

Trust role: a hash binds a verdict to a specific tree, capture
set, strategy, or binary. Every hashing function must be deterministic with
respect to file ordering and path normalization.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

SUBJECT_KINDS = ("tree", "frozen", "capture_set", "strategy", "binary", "outputs")


@dataclass(frozen=True)
class Subject:
    kind: str
    sha256: str

    def __post_init__(self):
        if self.kind not in SUBJECT_KINDS:
            raise ValueError(f"unknown subject kind: {self.kind!r}")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, d: dict) -> "Subject":
        return cls(kind=d["kind"], sha256=d["sha256"])


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def hash_files(files: list[dict]) -> str:
    """sha256 over (path, content) pairs, sorted by normalized path.

    `files` is a list of {"path": str, "content": str|bytes}. This is the
    same scheme demo/orchestrator/orchestrator.py's `src_sha` already uses
    (sorted by path, path bytes then content bytes, no separator) so a hash
    computed here reduces to the same value if truncated the same way.
    """
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: _normalize_path(x["path"])):
        h.update(_normalize_path(f["path"]).encode("utf-8"))
        content = f["content"]
        content_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
        h.update(content_bytes)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    """sha256 over a single blob (a strategy file, a binary, ...).

    Matches demo/oracle/app.py's POLICY_SHA scheme: plain sha256 of the raw
    file bytes, nothing else mixed in.
    """
    return hashlib.sha256(data).hexdigest()


def tree_subject(files: list[dict]) -> Subject:
    return Subject(kind="tree", sha256=hash_files(files))


def frozen_subject(files: list[dict]) -> Subject:
    return Subject(kind="frozen", sha256=hash_files(files))


def capture_set_subject(files: list[dict]) -> Subject:
    return Subject(kind="capture_set", sha256=hash_files(files))


def strategy_subject(data: bytes) -> Subject:
    return Subject(kind="strategy", sha256=hash_bytes(data))


def binary_subject(data: bytes) -> Subject:
    return Subject(kind="binary", sha256=hash_bytes(data))


def outputs_subject(cases: dict) -> Subject:
    """`cases` is {case_name: {var_name: bytes}}."""
    files = [
        {"path": f"{case}/{var}", "content": data}
        for case, vars_ in cases.items()
        for var, data in vars_.items()
    ]
    return Subject(kind="outputs", sha256=hash_files(files))
