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

A manifest has two forms. The minimal one -- `version`, `name`, and
`source` -- is enough to seed a baseline and start bringing a new code
in; the other six sections are what that work produces, and a manifest
holding all of them is `complete`. Nothing that checks a port will run
against a minimal manifest. Holding some but not all six is neither
form and is refused, naming what is absent, because it is far more
likely to be a half-written file than a choice anyone made.

`source.root` is relative to the manifest's own directory, so a code
directory can be moved or mounted anywhere without editing the file.
Every other path -- the makefile, the tolerance policy, the properties
module -- is relative to the source tree root, so the same text reads
the same whether the manifest sits beside that tree or inside it.

Complete manifests may set `timing.performance.min_median_speedup` for the
accepted port's baseline-median / port-median floor.  It defaults to 1.10 so
older complete manifests have an explicit, reviewable policy rather than
silently accepting any speedup.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path, PurePosixPath

import yaml

from equivalent.capture.variables import DTYPES, MAX_RANK, Variable
from equivalent.ledger.loading import check_keys
from equivalent.ledger.subjects import Subject, glob_matches, hash_bytes

VERSION = 1

# What every manifest says, in both forms.
REQUIRED_FIELDS = ("version", "name", "source")
# What a manifest gains when a code has been brought in far enough to be
# ported: all six together, or none of them.
COMPLETING_FIELDS = (
    "build", "interface", "datasets", "timing", "tolerances", "properties",
)

# Where a manifest lives while it is being written: inside the tree it
# describes, beside the driver and the tolerances it names. Spelled once
# here so the components that read it and the tree that carries it never
# disagree.
IN_TREE_MANIFEST = "harness/manifest.yaml"
# And what such a manifest says its source root is: the tree itself.
IN_TREE_SOURCE_ROOT = "."
REQUIRED_SOURCE_FIELDS = ("root", "patterns")
REQUIRED_BUILD_FIELDS = ("makefile", "targets")
REQUIRED_TARGET_FIELDS = ("target", "executable")
OPTIONAL_TARGET_FIELDS = ("runtime_artifacts",)
REQUIRED_RUNTIME_ARTIFACT_FIELDS = ("path", "kind")
OPTIONAL_RUNTIME_ARTIFACT_FIELDS = ("when_language",)
RUNTIME_ARTIFACT_KINDS = ("shared_library", "gpu_module")
RUNTIME_ARTIFACT_LANGUAGES = ("fortran", "c", "cxx", "cuda", "ptx")
REQUIRED_INTERFACE_FIELDS = ("module", "entry", "files", "inputs", "outputs")
REQUIRED_VARIABLE_FIELDS = ("name", "dtype", "rank")
REQUIRED_DATASET_FIELDS = ("args",)
REQUIRED_TIMING_FIELDS = ("args", "outputs", "budget_s")
# The timing run may need a few environment variables set to be a fair
# measurement. They are values, not code: strings in, strings out.
OPTIONAL_TIMING_FIELDS = ("env", "performance")
DEFAULT_MIN_MEDIAN_SPEEDUP = 1.10

# The build target every code must offer: the replay driver is what every
# regression check runs. `timing` and `capture` are named the same way but
# are not needed to load a manifest.
REQUIRED_BUILD_TARGET = "replay"
# The two datasets a port is judged against. Others may be declared; these
# two must be, and they must not be the same run twice.
REQUIRED_DATASETS = ("visible", "holdout")


@dataclass(frozen=True)
class Source:
    root: Path  # the source tree, resolved against the manifest's directory
    patterns: tuple


@dataclass(frozen=True)
class RuntimeArtifact:
    path: str  # file the executable loads at runtime, relative to the tree
    kind: str  # shared_library or gpu_module
    when_language: str | None = None


@dataclass(frozen=True)
class BuildTarget:
    target: str  # what `make` is asked for
    executable: str  # what that target leaves in the tree
    runtime_artifacts: tuple[RuntimeArtifact, ...] = ()


@dataclass(frozen=True)
class Build:
    makefile: str  # relative to the tree root
    targets: dict[str, BuildTarget]  # keyed by role


@dataclass(frozen=True)
class Interface:
    module: str
    entry: str
    # The files that implement the region, relative to the tree root and
    # spelled the way the tree spells them: what a port edits, and what
    # the harness's own self-check mutates.
    files: tuple
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
    min_median_speedup: float  # baseline median / port median required for acceptance


@dataclass(frozen=True)
class Manifest:
    version: int
    name: str
    source: Source
    # The six a minimal manifest leaves out. They are None together or
    # present together; the loader accepts no state in between.
    build: Build | None
    interface: Interface | None
    datasets: dict[str, Dataset] | None
    timing: Timing | None
    tolerances: Path | None  # resolved against the source tree root
    properties: Path | None  # a pytest module of invariants, or none declared
    sha256: str

    @property
    def complete(self) -> bool:
        """Does this manifest describe a code well enough to check a port of it.

        `properties` is left out of the test on purpose: a complete
        manifest writes `properties: null` when the code has no property
        module, so that field being None says nothing about which form
        this manifest is in.
        """
        return None not in (self.build, self.interface, self.datasets, self.timing, self.tolerances)

    def missing_parts(self) -> list:
        """What a minimal manifest still has to gain before a port can be checked.

        The loader takes all six or none, so this is either the whole
        list or empty -- there is no half-described code to report.
        """
        return [] if self.complete else list(COMPLETING_FIELDS)

    def as_subject(self) -> Subject:
        return Subject(kind="manifest", sha256=self.sha256)


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} is {value!r}; it must be a non-empty name")
    return value


def _resolve(directory: Path, value, where: str) -> Path:
    return directory / _name(value, where)


def _relative_file(value, where: str) -> str:
    """A normalized relative POSIX file path, with no route outside the tree."""
    name = _name(value, where)
    path = PurePosixPath(name)
    if (
        "\\" in name or path.is_absolute() or path == PurePosixPath(".")
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != name
    ):
        raise ValueError(f"{where} is {value!r}; it must be a normalized path inside the tree")
    return name


def _load_source(raw: dict, directory: Path, where: str) -> Source:
    check_keys(raw, REQUIRED_SOURCE_FIELDS, f"{where} source")
    root = _resolve(directory, raw["root"], f"{where} source root")
    if not root.is_dir():
        raise ValueError(f"{where} source root {raw['root']!r} is not a directory ({root})")
    patterns = tuple(_name(p, f"{where} source pattern") for p in raw["patterns"])
    if not patterns:
        raise ValueError(f"{where} source patterns is empty, so no file would count as source")
    return Source(root=root, patterns=patterns)


def _load_build(raw: dict, where: str) -> Build:
    check_keys(raw, REQUIRED_BUILD_FIELDS, f"{where} build")
    targets = {}
    for role, spec in raw["targets"].items():
        target_where = f"{where} build target '{role}'"
        check_keys(spec, REQUIRED_TARGET_FIELDS, target_where, optional=OPTIONAL_TARGET_FIELDS)
        raw_artifacts = spec.get("runtime_artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise ValueError(f"{target_where} runtime_artifacts must be a list")
        runtime_artifacts = []
        for artifact in raw_artifacts:
            artifact_where = f"{target_where} runtime artifact"
            if not isinstance(artifact, dict):
                raise ValueError(f"{artifact_where} must be an object")
            check_keys(
                artifact, REQUIRED_RUNTIME_ARTIFACT_FIELDS, artifact_where,
                optional=OPTIONAL_RUNTIME_ARTIFACT_FIELDS,
            )
            kind = artifact["kind"]
            if kind not in RUNTIME_ARTIFACT_KINDS:
                raise ValueError(
                    f"{artifact_where} kind is {kind!r}; it must be one of "
                    f"{list(RUNTIME_ARTIFACT_KINDS)}"
                )
            when_language = artifact.get("when_language")
            if when_language is not None and when_language not in RUNTIME_ARTIFACT_LANGUAGES:
                raise ValueError(
                    f"{artifact_where} when_language is {when_language!r}; it must be one of "
                    f"{list(RUNTIME_ARTIFACT_LANGUAGES)}"
                )
            runtime_artifacts.append(RuntimeArtifact(
                path=_relative_file(artifact["path"], f"{artifact_where} path"),
                kind=kind,
                when_language=when_language,
            ))
        artifact_paths = [artifact.path for artifact in runtime_artifacts]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError(f"{target_where} names the same runtime artifact more than once")
        executable = _relative_file(spec["executable"], f"{target_where} executable")
        if executable in artifact_paths:
            raise ValueError(f"{target_where} names its executable as a runtime artifact")
        targets[role] = BuildTarget(
            target=_name(spec["target"], f"{target_where} target"),
            executable=executable,
            runtime_artifacts=tuple(runtime_artifacts),
        )
    if REQUIRED_BUILD_TARGET not in targets:
        raise ValueError(
            f"{where} build targets are {sorted(targets)}; every code must offer "
            f"'{REQUIRED_BUILD_TARGET}', which is what the regression checks run"
        )
    return Build(makefile=_name(raw["makefile"], f"{where} build makefile"), targets=targets)


def _load_variable(raw: dict, where: str) -> Variable:
    check_keys(raw, REQUIRED_VARIABLE_FIELDS, where)
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
    check_keys(raw, REQUIRED_INTERFACE_FIELDS, f"{where} interface")
    files = tuple(_name(f, f"{where} interface file") for f in raw["files"])
    if not files:
        # A region nobody can point at is a region nobody can port or
        # mutate, and an empty list is far likelier to be a half-written
        # manifest than a decision.
        raise ValueError(
            f"{where} interface files is empty, so nothing names the code that "
            f"implements the region"
        )
    return Interface(
        module=_name(raw["module"], f"{where} interface module"),
        entry=_name(raw["entry"], f"{where} interface entry"),
        files=files,
        inputs=tuple(_load_variable(v, f"{where} interface input") for v in raw["inputs"]),
        outputs=tuple(_load_variable(v, f"{where} interface output") for v in raw["outputs"]),
    )


def _load_datasets(raw: dict, where: str) -> dict:
    # Named datasets beyond the two required ones are allowed, so a code
    # can declare more without this reader being taught each name.
    check_keys(raw, REQUIRED_DATASETS, f"{where} datasets", allow_extra=True)
    datasets = {}
    for name, spec in raw.items():
        dataset_where = f"{where} dataset '{name}'"
        check_keys(spec, REQUIRED_DATASET_FIELDS, dataset_where)
        datasets[name] = Dataset(args=tuple(str(a) for a in spec["args"]))
    if datasets["visible"].args == datasets["holdout"].args:
        raise ValueError(
            f"{where} datasets visible and holdout are the same run "
            f"{list(datasets['visible'].args)}; a holdout generated from the visible "
            f"parameters holds nothing back"
        )
    return datasets


def _load_timing(raw: dict, where: str) -> Timing:
    check_keys(raw, REQUIRED_TIMING_FIELDS, f"{where} timing", optional=OPTIONAL_TIMING_FIELDS)
    budget = raw["budget_s"]
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        raise ValueError(f"{where} timing budget_s is {budget!r}; it must be a positive number")
    return Timing(
        args=tuple(str(a) for a in raw["args"]),
        outputs=tuple(_name(o, f"{where} timing output") for o in raw["outputs"]),
        budget_s=budget,
        env=_load_timing_env(raw.get("env"), f"{where} timing env"),
        min_median_speedup=_load_min_median_speedup(
            raw.get("performance"), f"{where} timing performance",
        ),
    )


def _load_min_median_speedup(raw, where: str) -> float:
    """The explicit acceptance floor, defaulted for pre-policy manifests."""
    if raw is None:
        return DEFAULT_MIN_MEDIAN_SPEEDUP
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is not an object")
    check_keys(raw, ("min_median_speedup",), where)
    value = raw["min_median_speedup"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{where} min_median_speedup is {value!r}; it must be a finite number above 1"
        )
    try:
        measured = float(value)
    except OverflowError:
        measured = float("inf")
    if not math.isfinite(measured) or measured <= 1:
        raise ValueError(
            f"{where} min_median_speedup is {value!r}; it must be a finite number above 1"
        )
    return measured


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


def _in_tree_path(root: Path, value, where: str) -> Path:
    """One of the manifest's paths, resolved and checked inside the source tree."""
    path = _resolve(root, value, where)
    if not path.is_file():
        raise ValueError(f"{where} {value!r} is not a file in the source tree ({path})")
    return path


def _completing_fields(raw: dict, where: str) -> bool:
    """Is this the complete form? Anything between the two forms is an error."""
    absent = [field for field in COMPLETING_FIELDS if field not in raw]
    if not absent:
        return True
    if len(absent) == len(COMPLETING_FIELDS):
        return False
    raise ValueError(
        f"{where} is missing field(s): {absent}. A manifest either says only "
        f"{list(REQUIRED_FIELDS)}, which is enough to seed a baseline and start "
        f"describing a code, or says all of {list(COMPLETING_FIELDS)} as well"
    )


def load_manifest(path, *, source_base=None) -> Manifest:
    """Read one code's manifest.

    `source.root` is resolved against `source_base`, which defaults to the
    manifest's own directory; every other path is resolved against the
    source tree root that names. Pass `source_base` for a manifest that
    sits inside the tree it describes rather than beside it --
    `load_tree_manifest` is the one caller that does.
    """
    path = Path(path)
    where = f"manifest {path}"
    raw_bytes = path.read_bytes()
    raw = yaml.safe_load(raw_bytes)
    check_keys(raw, REQUIRED_FIELDS, where, optional=COMPLETING_FIELDS)
    if raw["version"] != VERSION:
        raise ValueError(f"{where} has version {raw['version']!r}; this reader understands {VERSION}")

    source = _load_source(
        raw["source"], Path(path.parent if source_base is None else source_base), where,
    )
    minimal = dict(
        version=raw["version"],
        name=_name(raw["name"], f"{where} name"),
        source=source,
        build=None, interface=None, datasets=None, timing=None,
        tolerances=None, properties=None,
        sha256=hash_bytes(raw_bytes),
    )
    if not _completing_fields(raw, where):
        return Manifest(**minimal)

    build = _load_build(raw["build"], where)
    _in_tree_path(source.root, build.makefile, f"{where} build makefile")

    interface = _load_interface(raw["interface"], where)
    for interface_file in interface.files:
        _in_tree_path(source.root, interface_file, f"{where} interface file")

    properties = None
    if raw["properties"] is not None:
        properties = _in_tree_path(source.root, raw["properties"], f"{where} properties")

    return Manifest(**{
        **minimal,
        "build": build,
        "interface": interface,
        "datasets": _load_datasets(raw["datasets"], where),
        "timing": _load_timing(raw["timing"], where),
        "tolerances": _in_tree_path(source.root, raw["tolerances"], f"{where} tolerances"),
        "properties": properties,
    })


def load_tree_manifest(tree_dir) -> Manifest:
    """Read the manifest a tree carries, at the one path a tree carries it.

    A manifest written inside the tree it describes says so: its source
    root is the tree itself. Anything else would name a directory outside
    the submission, which is not something a tree gets to do.
    """
    tree_dir = Path(tree_dir)
    manifest = load_manifest(tree_dir / IN_TREE_MANIFEST, source_base=tree_dir)
    if manifest.source.root.resolve() != tree_dir.resolve():
        raise ValueError(
            f"the manifest at {IN_TREE_MANIFEST} has a source root outside the tree "
            f"({manifest.source.root}); a manifest in the tree must say "
            f"source root {IN_TREE_SOURCE_ROOT!r}, which is the tree itself"
        )
    return manifest


# The builder has its own copy of the matching rule, because it runs in
# an image this package is not installed in, and a test compares the two
# answer for answer under the name the rule had while it lived here.
_matches = glob_matches


def source_files(manifest: Manifest, paths) -> list:
    """Of the given paths, the ones this code counts as source.

    The order given is the order returned, so a caller that sorted its
    paths keeps that order.
    """
    return [p for p in paths if any(glob_matches(p, pattern) for pattern in manifest.source.patterns)]
