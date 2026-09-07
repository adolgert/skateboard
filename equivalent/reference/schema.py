"""Strict, external contract for comparing onboarding with the original code.

The reference directory and this file belong to the reviewer, never the
submitted tree. Each load snapshots the source bytes used for both hashing
and building, so the recorded identity describes the actual build input.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path, PurePosixPath

import yaml

from equivalent.ledger.subjects import hash_files


def _keys(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("original reference entries must be mappings")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"original reference fields: missing {sorted(missing)}, unknown {sorted(extra)}")


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("original reference paths must be nonempty POSIX relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError(f"unsafe original reference path: {value!r}")
    return value


def _args(value):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("original reference arguments must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class OriginalReference:
    sha256: str
    files: tuple[dict, ...]
    makefile: str
    target: str
    executable: str
    source_patterns: tuple[str, ...]
    runs: tuple[dict, ...]
    provenance: str
    source_root: Path


def load_reference(path) -> OriginalReference:
    path = Path(path)
    contract = path.read_bytes()
    raw = yaml.safe_load(contract)
    _keys(raw, ("version", "provenance", "source", "build", "runs"))
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("original reference version must be 1")
    if not isinstance(raw["provenance"], str) or not raw["provenance"].strip():
        raise ValueError("original reference provenance must identify the reviewed original snapshot")
    _keys(raw["source"], ("root", "patterns"))
    root = (path.parent / _relative(raw["source"]["root"])).resolve()
    if not root.is_relative_to(path.parent.resolve()):
        raise ValueError("original reference source must stay beneath the contract directory")
    if not root.is_dir():
        raise ValueError(f"original reference source directory does not exist: {root}")
    patterns = raw["source"]["patterns"]
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("original reference needs source patterns")
    for pattern in patterns:
        _relative(pattern)
    files = []
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise ValueError(f"original reference snapshot must contain no symlinks: {entry}")
        if entry.is_file():
            rel = entry.relative_to(root).as_posix()
            if ".git" in entry.relative_to(root).parts:
                raise ValueError("original reference must be an exported snapshot without .git")
            files.append({"path": rel, "content": entry.read_bytes()})
    if not files:
        raise ValueError("original reference source snapshot is empty")
    _keys(raw["build"], ("makefile", "target", "executable"))
    build = raw["build"]
    for key in ("makefile", "executable"):
        _relative(build[key])
    if not isinstance(build["target"], str) or not build["target"] or build["target"].startswith("-"):
        raise ValueError("original reference build target must be a non-option string")
    if build["makefile"] not in {f["path"] for f in files}:
        raise ValueError("original reference Makefile is missing from its source snapshot")
    if not isinstance(raw["runs"], list) or not raw["runs"]:
        raise ValueError("original reference needs at least one comparison run")
    names = set()
    runs = []
    for run in raw["runs"]:
        _keys(run, ("name", "original_args", "candidate_args", "outputs"), ("env", "budget_s"))
        if not isinstance(run["name"], str) or not run["name"] or run["name"] in names:
            raise ValueError("original reference run names must be nonempty and unique")
        names.add(run["name"])
        _args(run["original_args"])
        _args(run["candidate_args"])
        env = run.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("original reference env must map strings to strings")
        budget = run.get("budget_s", 300)
        if type(budget) not in (int, float) or not math.isfinite(budget) or not 0 < budget <= 3600:
            raise ValueError("original reference budget_s must be positive and at most 3600")
        if not isinstance(run["outputs"], list) or not run["outputs"]:
            raise ValueError("each original reference run must compare outputs")
        for output in run["outputs"]:
            _keys(output, ("original", "candidate", "comparison"), ("tolerance",))
            _relative(output["original"])
            _relative(output["candidate"])
            if output["comparison"] not in ("bytes", "array_exact", "array_tolerance"):
                raise ValueError("unknown original reference comparison")
            tolerance = output.get("tolerance")
            if output["comparison"] == "array_tolerance":
                _keys(tolerance, ("abs", "rel", "ulp"))
                for key, value in tolerance.items():
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("reference tolerance must be finite and nonnegative")
                    if key == "ulp" and type(value) is not int:
                        raise ValueError("reference ULP tolerance must be an integer")
            elif tolerance is not None:
                raise ValueError("tolerance is only valid for array_tolerance comparisons")
        runs.append({**run, "env": env, "budget_s": budget})
    fingerprint = hash_files([
        {"path": "contract.yaml", "content": contract},
        *({"path": "source/" + f["path"], "content": f["content"]} for f in files),
    ])
    return OriginalReference(fingerprint, tuple(files), build["makefile"], build["target"],
                             build["executable"], tuple(patterns), tuple(runs), raw["provenance"], root)


def fingerprint_reference(path) -> str:
    return load_reference(path).sha256
