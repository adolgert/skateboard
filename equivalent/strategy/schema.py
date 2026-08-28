"""The strategy file: everything that depends on the strategy for porting.

Loading a wrong or stale strategy silently changes what gets compiled, what
counts as proof the code ran on the GPU, and which files a region is allowed
to touch.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from equivalent.ledger.subjects import Subject, hash_bytes

REQUIRED_FIELDS = (
    "name", "version", "allow_globs", "languages", "link_flags",
    "required_tools", "device_proof", "sanitizers", "analyzer_command",
)
REQUIRED_DEVICE_PROOF_FIELDS = ("notify", "mandatory")


@dataclass(frozen=True)
class Language:
    compiler: str
    flags: tuple

    @classmethod
    def from_dict(cls, d: dict) -> "Language":
        return cls(compiler=d["compiler"], flags=tuple(d["flags"]))


@dataclass(frozen=True)
class DeviceProof:
    notify: str | None  # "acc" | "omp" | None
    mandatory: bool

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceProof":
        missing = [f for f in REQUIRED_DEVICE_PROOF_FIELDS if f not in d]
        if missing:
            raise ValueError(f"strategy device_proof missing field(s): {missing}")
        return cls(notify=d["notify"], mandatory=bool(d["mandatory"]))


@dataclass(frozen=True)
class Strategy:
    name: str
    version: int
    allow_globs: tuple
    languages: dict  # {language_name: Language}
    link_flags: tuple
    required_tools: tuple
    device_proof: DeviceProof
    sanitizers: tuple
    analyzer_command: str
    sha256: str

    def allows(self, path: str) -> bool:
        """Would a region under this strategy be allowed to submit this path.

        Uses fnmatch, where "*" matches across "/" (no directory-boundary
        meaning) -- "src/*.f90" already matches any depth under src/, "**" is
        neither needed nor given any special handling.
        """
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.allow_globs)

    def rejected_paths(self, paths) -> list:
        """Of the given concrete paths, the ones this strategy does not allow."""
        return [p for p in paths if not self.allows(p)]

    def as_subject(self) -> Subject:
        return Subject(kind="strategy", sha256=self.sha256)


def load_strategy(path) -> Strategy:
    path = Path(path)
    raw = path.read_bytes()
    d = yaml.safe_load(raw)

    missing = [f for f in REQUIRED_FIELDS if f not in d]
    if missing:
        raise ValueError(f"strategy file {path} missing field(s): {missing}")

    languages = {lang: Language.from_dict(spec) for lang, spec in d["languages"].items()}

    return Strategy(
        name=d["name"],
        version=d["version"],
        allow_globs=tuple(d["allow_globs"]),
        languages=languages,
        link_flags=tuple(d["link_flags"]),
        required_tools=tuple(d["required_tools"]),
        device_proof=DeviceProof.from_dict(d["device_proof"]),
        sanitizers=tuple(d["sanitizers"]),
        analyzer_command=d["analyzer_command"],
        sha256=hash_bytes(raw),
    )
