"""Strict, external contract for comparing onboarding with the original code.

The reference directory and this file belong to the reviewer, never the
submitted tree. Each load snapshots the source bytes used for both hashing
and building, so the recorded identity describes the actual build input.

Trust role: this is the only description of what the onboarded program is
compared against. A field read loosely here would let a comparison be
made against different arguments, a different budget, or a looser
tolerance than the reviewer wrote, and the claim would still say the two
programs agreed. So the file is read section by section, and every
failure names the section it was reading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path, PurePosixPath

import yaml

from equivalent.ledger.loading import check_keys
from equivalent.ledger.subjects import hash_files

VERSION = 1
# How long one comparison run may take, when the contract does not say.
DEFAULT_BUDGET_S = 300
MAX_BUDGET_S = 3600
# How the two programs' answers for one output may be compared. Bytes is
# the strictest; a tolerance band is only meaningful for the last.
COMPARISONS = ("bytes", "array_exact", "array_tolerance")
TOLERANCE_FIELDS = ("abs", "rel", "ulp")


def _relative(value, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{where} is {value!r}; it must be a nonempty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError(f"{where} is unsafe: {value!r}")
    return value


def _args(value, where: str) -> tuple:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where} is {value!r}; it must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class Output:
    """One file the two programs both write, and how they are compared."""

    original: str
    candidate: str
    comparison: str
    tolerance: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "original": self.original,
            "candidate": self.candidate,
            "comparison": self.comparison,
        }
        if self.tolerance is not None:
            out["tolerance"] = dict(self.tolerance)
        return out


@dataclass(frozen=True)
class Run:
    """One comparison: both programs run once, and their outputs weighed."""

    name: str
    original_args: tuple
    candidate_args: tuple
    env: dict = field(default_factory=dict)
    budget_s: float = DEFAULT_BUDGET_S
    outputs: tuple = ()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "original_args": self.original_args,
            "candidate_args": self.candidate_args,
            "env": dict(self.env),
            "budget_s": self.budget_s,
            "outputs": [output.as_dict() for output in self.outputs],
        }


@dataclass(frozen=True)
class OriginalReference:
    sha256: str
    files: tuple[dict, ...]
    makefile: str
    target: str
    executable: str
    source_patterns: tuple[str, ...]
    # The runs as mappings, which is what the check that walks them
    # reads: `run["name"]`, its two argument lists, its env, its budget,
    # and `run["outputs"]`, each a mapping with an original, a candidate,
    # a comparison, and sometimes a tolerance. They are built from the
    # `Run` and `Output` values above, so this is the shape to drop once
    # that check reads the values themselves.
    runs: tuple[dict, ...]
    provenance: str
    source_root: Path


def _load_source(raw, contract_dir: Path, where: str) -> tuple[Path, tuple[str, ...]]:
    """The reviewed snapshot's root and the patterns that say what it builds."""
    check_keys(raw, ("root", "patterns"), where)
    root = (contract_dir / _relative(raw["root"], f"{where} root")).resolve()
    if not root.is_relative_to(contract_dir.resolve()):
        raise ValueError(f"{where} must stay beneath the contract directory")
    if not root.is_dir():
        raise ValueError(f"{where} directory does not exist: {root}")
    patterns = raw["patterns"]
    if not isinstance(patterns, list) or not patterns:
        raise ValueError(f"{where} needs at least one source pattern")
    return root, tuple(_relative(p, f"{where} pattern") for p in patterns)


def _snapshot(root: Path, where: str) -> tuple[dict, ...]:
    """Every file of the reviewed snapshot, read once and hashed as read."""
    files = []
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise ValueError(f"{where} must contain no symlinks: {entry}")
        if entry.is_file():
            relative = entry.relative_to(root)
            if ".git" in relative.parts:
                raise ValueError(f"{where} must be an exported snapshot without .git")
            files.append({"path": relative.as_posix(), "content": entry.read_bytes()})
    if not files:
        raise ValueError(f"{where} is empty")
    return tuple(files)


def _load_build(raw, files: tuple[dict, ...], where: str) -> dict:
    """What builds the original: its makefile, its target, and what it leaves."""
    check_keys(raw, ("makefile", "target", "executable"), where)
    makefile = _relative(raw["makefile"], f"{where} makefile")
    executable = _relative(raw["executable"], f"{where} executable")
    target = raw["target"]
    if not isinstance(target, str) or not target or target.startswith("-"):
        raise ValueError(f"{where} target is {target!r}; it must be a non-option string")
    if makefile not in {f["path"] for f in files}:
        raise ValueError(f"{where} makefile is missing from its source snapshot")
    return {"makefile": makefile, "target": target, "executable": executable}


def _load_tolerance(raw, comparison: str, where: str) -> dict | None:
    """The band this output is compared within, refused where it means nothing."""
    if comparison != "array_tolerance":
        if raw is not None:
            raise ValueError(f"{where} is only valid for array_tolerance comparisons")
        return None
    check_keys(raw, TOLERANCE_FIELDS, where)
    for key, value in raw.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{where} {key} must be finite and nonnegative")
        if key == "ulp" and type(value) is not int:
            raise ValueError(f"{where} ulp must be an integer")
    return dict(raw)


def _load_output(raw, where: str) -> Output:
    check_keys(raw, ("original", "candidate", "comparison"), where, optional=("tolerance",))
    comparison = raw["comparison"]
    if comparison not in COMPARISONS:
        raise ValueError(
            f"{where} comparison is {comparison!r}; it must be one of {list(COMPARISONS)}"
        )
    return Output(
        original=_relative(raw["original"], f"{where} original"),
        candidate=_relative(raw["candidate"], f"{where} candidate"),
        comparison=comparison,
        tolerance=_load_tolerance(raw.get("tolerance"), comparison, f"{where} tolerance"),
    )


def _load_env(raw, where: str) -> dict:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ValueError(f"{where} must map strings to strings")
    return dict(raw)


def _load_budget(raw, where: str) -> float:
    if type(raw) not in (int, float) or not math.isfinite(raw) or not 0 < raw <= MAX_BUDGET_S:
        raise ValueError(f"{where} must be positive and at most {MAX_BUDGET_S}")
    return raw


def _load_run(raw, taken: set, where: str) -> Run:
    check_keys(
        raw, ("name", "original_args", "candidate_args", "outputs"), where,
        optional=("env", "budget_s"),
    )
    name = raw["name"]
    if not isinstance(name, str) or not name or name in taken:
        raise ValueError(f"{where} name is {name!r}; run names must be nonempty and unique")
    taken.add(name)
    outputs = raw["outputs"]
    if not isinstance(outputs, list) or not outputs:
        raise ValueError(f"{where} must compare at least one output")
    return Run(
        name=name,
        original_args=_args(raw["original_args"], f"{where} original_args"),
        candidate_args=_args(raw["candidate_args"], f"{where} candidate_args"),
        env=_load_env(raw.get("env", {}), f"{where} env"),
        budget_s=_load_budget(raw.get("budget_s", DEFAULT_BUDGET_S), f"{where} budget_s"),
        outputs=tuple(
            _load_output(output, f"{where} output {i}") for i, output in enumerate(outputs)
        ),
    )


def _load_runs(raw, where: str) -> tuple[Run, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where} needs at least one comparison run")
    taken: set = set()
    return tuple(_load_run(run, taken, f"{where} run {i}") for i, run in enumerate(raw))


def load_reference(path) -> OriginalReference:
    """Read the reviewer's contract and snapshot the source it names.

    The snapshot is read here, not later: the identity this returns is a
    hash of the contract and those exact bytes, and it is what the
    original comparison is built from, so nothing can change between
    being hashed and being built.
    """
    path = Path(path)
    where = f"original reference {path}"
    contract = path.read_bytes()
    raw = yaml.safe_load(contract)
    check_keys(raw, ("version", "provenance", "source", "build", "runs"), where)

    if type(raw["version"]) is not int or raw["version"] != VERSION:
        raise ValueError(f"{where} version must be {VERSION}")
    if not isinstance(raw["provenance"], str) or not raw["provenance"].strip():
        raise ValueError(f"{where} provenance must identify the reviewed original snapshot")

    root, patterns = _load_source(raw["source"], path.parent, f"{where} source")
    files = _snapshot(root, f"{where} source snapshot")
    build = _load_build(raw["build"], files, f"{where} build")
    runs = _load_runs(raw["runs"], where)

    return OriginalReference(
        sha256=hash_files([
            {"path": "contract.yaml", "content": contract},
            *({"path": "source/" + f["path"], "content": f["content"]} for f in files),
        ]),
        files=files,
        makefile=build["makefile"],
        target=build["target"],
        executable=build["executable"],
        source_patterns=patterns,
        runs=tuple(run.as_dict() for run in runs),
        provenance=raw["provenance"],
        source_root=root,
    )


def fingerprint_reference(path) -> str:
    return load_reference(path).sha256
