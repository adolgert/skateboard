"""What a claim or artifact is about.

Trust role: a hash binds a verdict to a specific tree, capture
set, strategy, or binary. Every hashing function must be deterministic with
respect to file ordering and path normalization.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# "policy" is the oracle's tolerance policy -- it appears in regression
# claims' materials (a pass under a loose policy must be distinguishable
# from a pass under a strict one), never as a claim's subject. "manifest"
# is the code's own description, and appears in every claim's materials
# for the same reason: a pass against one code must not read as a pass
# against another.
SUBJECT_KINDS = (
    "tree", "frozen", "capture_set", "strategy", "manifest", "binary", "outputs", "policy",
    "evidence_policy", "reference", "executor", "oracle",
)

# A claim made before this policy existed is readable history, but is not
# evidence for the current acceptance decision.  Bump this value whenever a
# change to the gateway/checker contract changes what a passing claim means.
EVIDENCE_POLICY_VERSION = 2


def is_digest(value) -> bool:
    """Whether this is a digest, written the one way everything writes one.

    Sixty-four lowercase hexadecimal characters and nothing else. A
    subject, a builder's executor identity, an oracle's identity, and a
    reviewed pin in a deployment file are all the same kind of name for
    the same kind of thing, so they are all held to this. Anything else
    is a value nobody may treat as naming a file or a service, and the
    caller says what it is going to do about that.
    """
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class Subject:
    kind: str
    sha256: str

    def __post_init__(self):
        if self.kind not in SUBJECT_KINDS:
            raise ValueError(f"unknown subject kind: {self.kind!r}")
        if not is_digest(self.sha256):
            raise ValueError("subject sha256 must be exactly 64 lowercase hexadecimal characters")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, d: dict) -> "Subject":
        return cls(kind=d["kind"], sha256=d["sha256"])


def _normalize_path(path: str) -> str:
    # Strip a "./" prefix, not a character class: str.lstrip("./") would
    # also turn ".gitignore" into "gitignore" and collide two distinct
    # trees into one hash.
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def glob_matches(path: str, pattern: str) -> bool:
    """Does one pattern cover this path. The only rule anyone matches by.

    A pattern is written once and read in several places: a strategy's
    allow-list decides what a session may edit, the same list decides
    what a submit accepts and which baseline files are frozen out of the
    hash, and a manifest's source patterns decide what counts as source.
    Two readers with two rules would allow an edit and then reject it, or
    freeze a file one of them thought was allowed to move, so all of them
    ask here.

    The rule: the path is normalized first, so "./src/a.f90" and
    "src/a.f90" are one path. Matching ignores case, because Fortran
    spells the same extension both ways and a tree may hold either. A
    leading "**/" means "at any depth, including none", so "**/*.f90"
    covers both mod_kernel.f90 and src/mod_kernel.f90; elsewhere "*"
    already crosses "/", so no other pattern needs the prefix.

    Both sides are lower-cased and then matched case-sensitively rather
    than leaving the choice to fnmatch, whose own case rule follows the
    operating system: this answers the same on every machine.
    """
    lowered = _normalize_path(path).lower()
    pattern = pattern.lower()
    if pattern.startswith("**/"):
        rest = pattern[3:]
        return fnmatch.fnmatchcase(lowered, rest) or fnmatch.fnmatchcase(lowered, f"*/{rest}")
    return fnmatch.fnmatchcase(lowered, pattern)


def hash_files(files: list[dict]) -> str:
    """sha256 over (path, content) pairs, sorted by normalized path.

    `files` is a list of {"path": str, "content": str|bytes}.
    Paths and contents are length-delimited.  Concatenating them directly is
    ambiguous: ("a", "bc") and ("ab", "c") otherwise hash to the same byte
    stream without requiring a SHA-256 collision.
    """
    h = hashlib.sha256()
    h.update(b"equivalent:file-set:v2\0")
    normalized = [(_normalize_path(f["path"]), f["content"]) for f in files]
    paths = [path for path, _ in normalized]
    if len(paths) != len(set(paths)):
        raise ValueError("file set contains duplicate normalized paths")
    h.update(struct.pack(">Q", len(normalized)))
    for path, content in sorted(normalized, key=lambda item: item[0]):
        path_bytes = path.encode("utf-8")
        content_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
        h.update(struct.pack(">Q", len(path_bytes)))
        h.update(path_bytes)
        h.update(struct.pack(">Q", len(content_bytes)))
        h.update(content_bytes)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    """sha256 over a single blob (a strategy file, a binary, ...).

    Matches services/oracle/app.py's POLICY_SHA scheme: plain sha256 of the raw
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


def policy_subject(policy_bytes: bytes) -> Subject:
    """The tolerance policy a comparison was judged within, as a material.

    Plain sha256 of the file's bytes, because this digest crosses the
    image boundary: the oracle computes it from the bytes it was given
    and puts it in the answer it returns, and a check puts the same
    digest on the claim it files. Anything else -- a framed file-set
    hash, a hash of the path with it -- would be a second name for one
    file, and a claim would carry two policy subjects for the policy it
    was actually judged under.

    A ledger written before this was settled carries the framed digest,
    which is not this one, so those claims read as stale under the
    current policy. That is what the material is for: a claim only counts
    as evidence when it names the inputs that are in force now.
    """
    return Subject(kind="policy", sha256=hash_bytes(policy_bytes))


def outputs_subject(cases: dict) -> Subject:
    """`cases` is {case_name: {var_name: bytes}}."""
    files = [
        {"path": f"{case}/{var}", "content": data}
        for case, vars_ in cases.items()
        for var, data in vars_.items()
    ]
    return Subject(kind="outputs", sha256=hash_files(files))


@lru_cache(maxsize=1)
def evidence_policy_subject() -> Subject:
    """Identity of the local trusted code and its evidence-contract version.

    The explicit version records an intentional semantics boundary.  Hashing
    the installed Python sources also prevents a checker edit that forgot to
    bump that version from silently reusing older claims.
    """
    package_root = Path(__file__).resolve().parents[1]
    files = [
        {
            "path": path.relative_to(package_root).as_posix(),
            "content": path.read_bytes(),
        }
        for path in sorted(package_root.rglob("*.py"))
        if "tests" not in path.relative_to(package_root).parts
    ]
    files.append({
        "path": "@evidence-contract-version",
        "content": str(EVIDENCE_POLICY_VERSION),
    })
    return Subject(
        kind="evidence_policy",
        sha256=hash_files(files),
    )
