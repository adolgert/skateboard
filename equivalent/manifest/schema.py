"""The code manifest: one hashed file saying what a code is.

Trust role: this is the only description of a code the gateway trusts.
It names the tree the baseline is read from, the build targets, the
region's interface, which parameters make the visible and the held-out
datasets, and the tolerance policy. A wrong entry here does not make a
check lie, but it makes every claim describe a different code from the
one the person thinks they are reviewing -- so the file is read
strictly: a missing field, an unknown key, a type the harness cannot
carry, or a path that is not on disk stops the load, naming what was
wrong. Its sha256 goes into every claim's materials, so a manifest
edited mid-session cannot be mistaken for the one the claims were filed
under.

Every path in the file is relative to the manifest's own directory, so a
code directory can be moved or mounted anywhere without editing it.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from equivalent.ledger.subjects import Subject, hash_bytes

VERSION = 1

REQUIRED_FIELDS = (
    "version", "name", "source", "build", "interface", "datasets",
    "timing", "tolerances", "properties",
)
REQUIRED_SOURCE_FIELDS = ("root", "patterns")
REQUIRED_BUILD_FIELDS = ("makefile", "targets")
REQUIRED_TARGET_FIELDS = ("target", "executable")
REQUIRED_INTERFACE_FIELDS = ("module", "entry", "inputs", "outputs")
REQUIRED_VARIABLE_FIELDS = ("name", "dtype", "rank")
REQUIRED_DATASET_FIELDS = ("args",)
REQUIRED_TIMING_FIELDS = ("args", "outputs", "budget_s")
# The timing run may need a few environment variables set to be a fair
# measurement. They are values, not code: strings in, strings out.
OPTIONAL_TIMING_FIELDS = ("env",)

# The build target every code must offer: the replay driver is what every
# regression check runs. `timing` and `capture` are named the same way but
# are not needed to load a manifest.
REQUIRED_BUILD_TARGET = "replay"
# The two datasets a port is judged against. Others may be declared; these
# two must be, and they must not be the same run twice.
REQUIRED_DATASETS = ("visible", "holdout")

# The types the capture format and the comparator can carry, spelled the
# way the manifest writes them. Anything else would reach the harness as a
# type no reader knows the width of.
DTYPES = ("f32", "f64", "i32", "i64", "l")
MAX_RANK = 4


@dataclass(frozen=True)
class Source:
    root: Path  # resolved against the manifest's directory
    patterns: tuple


@dataclass(frozen=True)
class BuildTarget:
    target: str  # what `make` is asked for
    executable: str  # what that target leaves in the tree


@dataclass(frozen=True)
class Build:
    makefile: str  # relative to the tree root
    targets: dict  # {role: BuildTarget}


@dataclass(frozen=True)
class Variable:
    name: str
    dtype: str  # one of DTYPES
    rank: int  # 0..MAX_RANK


@dataclass(frozen=True)
class Interface:
    module: str
    entry: str
    inputs: tuple  # (Variable, ...)
    outputs: tuple


@dataclass(frozen=True)
class Dataset:
    args: tuple  # the command line the capture program is given


@dataclass(frozen=True)
class Timing:
    args: tuple
    outputs: tuple  # files the timing run writes, compared per port
    budget_s: int
    env: dict  # {name: value} added to the timing run's environment


@dataclass(frozen=True)
class Manifest:
    version: int
    name: str
    source: Source
    build: Build
    interface: Interface
    datasets: dict  # {name: Dataset}
    timing: Timing
    tolerances: Path  # resolved against the manifest's directory
    properties: Path | None  # a pytest module of invariants, or none
    sha256: str

    def as_subject(self) -> Subject:
        return Subject(kind="manifest", sha256=self.sha256)


def _check_keys(given, required, where: str, *, optional=(), allow_extra: bool = False) -> None:
    if not isinstance(given, dict):
        raise ValueError(f"{where} is not a mapping")
    missing = [field for field in required if field not in given]
    if missing:
        raise ValueError(f"{where} missing field(s): {missing}")
    if allow_extra:
        return
    unknown = sorted(set(given) - set(required) - set(optional))
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s): {unknown}; allowed: {sorted((*required, *optional))}"
        )


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} is {value!r}; it must be a non-empty name")
    return value


def _resolve(directory: Path, value, where: str) -> Path:
    return directory / _name(value, where)


def _load_source(raw: dict, directory: Path, where: str) -> Source:
    _check_keys(raw, REQUIRED_SOURCE_FIELDS, f"{where} source")
    root = _resolve(directory, raw["root"], f"{where} source root")
    if not root.is_dir():
        raise ValueError(f"{where} source root {raw['root']!r} is not a directory ({root})")
    patterns = tuple(_name(p, f"{where} source pattern") for p in raw["patterns"])
    if not patterns:
        raise ValueError(f"{where} source patterns is empty, so no file would count as source")
    return Source(root=root, patterns=patterns)


def _load_build(raw: dict, where: str) -> Build:
    _check_keys(raw, REQUIRED_BUILD_FIELDS, f"{where} build")
    targets = {}
    for role, spec in raw["targets"].items():
        target_where = f"{where} build target '{role}'"
        _check_keys(spec, REQUIRED_TARGET_FIELDS, target_where)
        targets[role] = BuildTarget(
            target=_name(spec["target"], f"{target_where} target"),
            executable=_name(spec["executable"], f"{target_where} executable"),
        )
    if REQUIRED_BUILD_TARGET not in targets:
        raise ValueError(
            f"{where} build targets are {sorted(targets)}; every code must offer "
            f"'{REQUIRED_BUILD_TARGET}', which is what the regression checks run"
        )
    return Build(makefile=_name(raw["makefile"], f"{where} build makefile"), targets=targets)


def _load_variable(raw: dict, where: str) -> Variable:
    _check_keys(raw, REQUIRED_VARIABLE_FIELDS, where)
    name = _name(raw["name"], f"{where} name")
    if raw["dtype"] not in DTYPES:
        raise ValueError(
            f"{where} variable '{name}' has dtype {raw['dtype']!r}; "
            f"it must be one of {list(DTYPES)}"
        )
    rank = raw["rank"]
    if not isinstance(rank, int) or isinstance(rank, bool) or not 0 <= rank <= MAX_RANK:
        raise ValueError(
            f"{where} variable '{name}' has rank {rank!r}; it must be a whole number "
            f"from 0 to {MAX_RANK}"
        )
    return Variable(name=name, dtype=raw["dtype"], rank=rank)


def _load_interface(raw: dict, where: str) -> Interface:
    _check_keys(raw, REQUIRED_INTERFACE_FIELDS, f"{where} interface")
    return Interface(
        module=_name(raw["module"], f"{where} interface module"),
        entry=_name(raw["entry"], f"{where} interface entry"),
        inputs=tuple(_load_variable(v, f"{where} interface input") for v in raw["inputs"]),
        outputs=tuple(_load_variable(v, f"{where} interface output") for v in raw["outputs"]),
    )


def _load_datasets(raw: dict, where: str) -> dict:
    # Named datasets beyond the two required ones are allowed, so a code
    # can declare more without this reader being taught each name.
    _check_keys(raw, REQUIRED_DATASETS, f"{where} datasets", allow_extra=True)
    datasets = {}
    for name, spec in raw.items():
        dataset_where = f"{where} dataset '{name}'"
        _check_keys(spec, REQUIRED_DATASET_FIELDS, dataset_where)
        datasets[name] = Dataset(args=tuple(str(a) for a in spec["args"]))
    if datasets["visible"].args == datasets["holdout"].args:
        raise ValueError(
            f"{where} datasets visible and holdout are the same run "
            f"{list(datasets['visible'].args)}; a holdout generated from the visible "
            f"parameters holds nothing back"
        )
    return datasets


def _load_timing(raw: dict, where: str) -> Timing:
    _check_keys(raw, REQUIRED_TIMING_FIELDS, f"{where} timing", optional=OPTIONAL_TIMING_FIELDS)
    budget = raw["budget_s"]
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        raise ValueError(f"{where} timing budget_s is {budget!r}; it must be a positive number")
    return Timing(
        args=tuple(str(a) for a in raw["args"]),
        outputs=tuple(_name(o, f"{where} timing output") for o in raw["outputs"]),
        budget_s=budget,
        env=_load_timing_env(raw.get("env"), f"{where} timing env"),
    )


def _load_timing_env(raw, where: str) -> dict:
    """The variables the timing run is given, defaulting to none.

    Values are required to be written as strings rather than quietly
    converted, because the ones that matter here look like numbers and
    are not: a YAML `-1` would arrive as an integer and a `1073741824`
    would round-trip through a float on some readers, and what reaches
    the program has to be exactly what the file says.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is not a mapping of names to values")
    env = {}
    for name, value in raw.items():
        env[_name(name, f"{where} name")] = _name(
            value, f"{where} value for '{name}' (write it in quotes)"
        )
    return env


def load_manifest(path) -> Manifest:
    """Read one code's manifest, resolving its paths against its own directory."""
    path = Path(path)
    where = f"manifest {path}"
    raw_bytes = path.read_bytes()
    raw = yaml.safe_load(raw_bytes)
    _check_keys(raw, REQUIRED_FIELDS, where)
    if raw["version"] != VERSION:
        raise ValueError(f"{where} has version {raw['version']!r}; this reader understands {VERSION}")

    directory = path.parent
    tolerances = _resolve(directory, raw["tolerances"], f"{where} tolerances")
    if not tolerances.is_file():
        raise ValueError(f"{where} tolerances {raw['tolerances']!r} is not a file ({tolerances})")

    properties = None
    if raw["properties"] is not None:
        properties = _resolve(directory, raw["properties"], f"{where} properties")
        if not properties.is_file():
            raise ValueError(f"{where} properties {raw['properties']!r} is not a file ({properties})")

    return Manifest(
        version=raw["version"],
        name=_name(raw["name"], f"{where} name"),
        source=_load_source(raw["source"], directory, where),
        build=_load_build(raw["build"], where),
        interface=_load_interface(raw["interface"], where),
        datasets=_load_datasets(raw["datasets"], where),
        timing=_load_timing(raw["timing"], where),
        tolerances=tolerances,
        properties=properties,
        sha256=hash_bytes(raw_bytes),
    )


def _normalized(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _matches(path: str, pattern: str) -> bool:
    """Does one source pattern cover this path.

    Matching ignores case, because Fortran spells the same extension both
    ways and the tree may hold either. A leading "**/" means "at any
    depth, including none", so "**/*.f90" covers both mod_kernel.f90 and
    src/mod_kernel.f90. Elsewhere "*" already crosses "/", so no other
    pattern needs the prefix.

    The comparison lower-cases both sides and then matches
    case-sensitively rather than leaving the choice to fnmatch, whose own
    case rule follows the operating system.
    """
    lowered = _normalized(path).lower()
    pattern = pattern.lower()
    if pattern.startswith("**/"):
        rest = pattern[3:]
        return fnmatch.fnmatchcase(lowered, rest) or fnmatch.fnmatchcase(lowered, f"*/{rest}")
    return fnmatch.fnmatchcase(lowered, pattern)


def source_files(manifest: Manifest, paths) -> list:
    """Of the given paths, the ones this code counts as source.

    The order given is the order returned, so a caller that sorted its
    paths keeps that order.
    """
    return [p for p in paths if any(_matches(p, pattern) for pattern in manifest.source.patterns)]
