"""The gateway's configuration file: one YAML naming every path and region.

Trust role: this decides which repository, which ledger, which working
copy, which code, and which strategy each region is checked against. A
wrong entry here does not make a check lie, but it makes every claim
describe something other than what the person thinks they are
reviewing. So the file is read strictly: an unknown key, a missing
field, a region naming a code the file does not describe, or a strategy
file that is not on disk stops the gateway at startup rather than
surfacing as a puzzling failure on the first request.

The file has three sections::

    version: 1
    paths:
      repo: /repo
      ledger_root: /ledger
      working_copy: /working
      programs: /programs
      strategies: /strategies
      seed: /seed
      sessions: /sessions
    codes:
      tsunami:
        manifest: tsunami/manifest.yaml
    regions:
      "ch04:step":
        code: tsunami
        spec_path: notes/regions/ch04-step.sese.yaml
        strategy: stdpar_managed
        baseline_strategy: cpu_reference
        visible_dataset: visible

Each region's directories are built by joining them: the ledger lives at
`<ledger_root>/<baseline commit>/<region id with ':' replaced by '-'>`,
the strategy at `<strategies>/<strategy>.yaml` (and the baseline
strategy the same way), a code's manifest at
`<programs>/<manifest>`, and the visible dataset at
`<programs>/<code>/datasets/<visible_dataset>`. Nothing spells those
layouts a second time.

A code's manifest path is relative to `programs` rather than absolute so
that the same file describes the deployment seen from inside the
container and from the host, where only the `paths` values differ.

`sessions` is the odd one out: it names where the agent's own session
transcripts are written, which nothing in the gateway ever reads. It is
here so that a tool reading a ledger can find the transcripts belonging
to the same deployment. A deployment whose transcripts live elsewhere
leaves the key out.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import baseline_commit, init_baseline_repo, region_slug
from equivalent.manifest.schema import Manifest, load_manifest

VERSION = 1

TOP_LEVEL_KEYS = ("version", "paths", "codes", "regions")
REQUIRED_PATH_KEYS = ("repo", "ledger_root", "working_copy", "programs", "strategies")
OPTIONAL_PATH_KEYS = ("seed", "sessions")
REQUIRED_CODE_KEYS = ("manifest",)
REQUIRED_REGION_KEYS = ("code", "spec_path", "strategy", "baseline_strategy")
OPTIONAL_REGION_KEYS = ("visible_dataset",)
# Where a code keeps the datasets a region may name, under its own
# directory. One spelling, so the deployment and this reader agree.
DATASETS_DIR = "datasets"


@dataclass(frozen=True)
class Paths:
    """The directories the gateway works in, as the file named them."""

    repo: Path
    ledger_root: Path
    working_copy: Path
    programs: Path
    strategies: Path
    seed: Path | None = None
    # Where the agent's own session transcripts are written. The gateway
    # never reads this -- it is here so the reading tools find the
    # transcripts of the same deployment the ledger belongs to, rather
    # than being told a second time on the command line. It is not
    # checked at load time, because the machine running the gateway need
    # not be the machine holding the transcripts.
    sessions: Path | None = None


@dataclass(frozen=True)
class CodeConfig:
    """One code the deployment holds, and the manifest describing it."""

    name: str
    manifest_path: Path
    manifest: Manifest


@dataclass(frozen=True)
class GatewayConfig:
    paths: Paths
    baseline_commit: str
    codes: dict[str, CodeConfig]
    regions: dict[str, RegionConfig]


def _check_keys(given, required, optional, where: str) -> None:
    missing = [key for key in required if key not in given]
    if missing:
        raise ValueError(f"{where} missing field(s): {missing}")
    unknown = sorted(set(given) - set(required) - set(optional))
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s): {unknown}; allowed: {sorted((*required, *optional))}"
        )


def _load_paths(raw: dict, where: str) -> Paths:
    _check_keys(raw, REQUIRED_PATH_KEYS, OPTIONAL_PATH_KEYS, f"{where} paths")
    optional = {key: Path(raw[key]) for key in OPTIONAL_PATH_KEYS if raw.get(key) is not None}
    return Paths(**{key: Path(raw[key]) for key in REQUIRED_PATH_KEYS}, **optional)


def _resolve_baseline_commit(paths: Paths, where: str, seed_if_empty: bool) -> str:
    """The baseline commit every region's ledger directory is filed under.

    When the repository has not been created yet and the caller asked for
    it, the seed directory becomes the baseline with one commit. Once the
    repository exists this never touches it again, so starting the
    gateway a second time keeps the same baseline and the same ledger.
    """
    if not (paths.repo / ".git").is_dir():
        if seed_if_empty and paths.seed is not None and paths.seed.is_dir():
            return init_baseline_repo(paths.repo, paths.seed)
        raise ValueError(
            f"{where}: repo {paths.repo} holds no git repository, and there is no "
            f"seed directory to build one from"
        )
    commit = baseline_commit(paths.repo)
    if commit is None:
        raise ValueError(
            f"{where}: repo {paths.repo} has no 'main' branch, so there is no baseline "
            f"commit to file the ledger under"
        )
    return commit


def _load_code(name: str, raw: dict, paths: Paths, where: str) -> CodeConfig:
    code_where = f"{where} code '{name}'"
    _check_keys(raw, REQUIRED_CODE_KEYS, (), code_where)
    manifest_path = paths.programs / raw["manifest"]
    if not manifest_path.is_file():
        raise ValueError(
            f"{code_where} names manifest '{raw['manifest']}', but {manifest_path} does not exist"
        )
    return CodeConfig(name=name, manifest_path=manifest_path, manifest=load_manifest(manifest_path))


def _strategy_path(name, field: str, paths: Paths, region_where: str) -> Path:
    path = paths.strategies / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(
            f"{region_where} names {field} '{name}', but {path} does not exist"
        )
    return path


def _load_region(
    region_id: str, raw: dict, paths: Paths, codes: dict, commit: str, where: str
) -> RegionConfig:
    region_where = f"{where} region '{region_id}'"
    _check_keys(raw, REQUIRED_REGION_KEYS, OPTIONAL_REGION_KEYS, region_where)

    code = codes.get(raw["code"])
    if code is None:
        raise ValueError(
            f"{region_where} names code '{raw['code']}', which the codes section does "
            f"not describe; it has {sorted(codes)}"
        )

    strategy_path = _strategy_path(raw["strategy"], "strategy", paths, region_where)
    # The baseline is built with a strategy of its own -- the comparison
    # floor a speedup is measured against. It is named per region rather
    # than fixed here, because what counts as a fair floor is a property
    # of the code and the machine, not of this reader.
    baseline_strategy_path = _strategy_path(
        raw["baseline_strategy"], "baseline_strategy", paths, region_where,
    )

    visible_dataset_dir = None
    if raw.get("visible_dataset") is not None:
        visible_dataset_dir = paths.programs / code.name / DATASETS_DIR / raw["visible_dataset"]
        if not visible_dataset_dir.is_dir():
            raise ValueError(
                f"{region_where} names visible_dataset '{raw['visible_dataset']}', but "
                f"{visible_dataset_dir} is not a directory"
            )

    return RegionConfig(
        region_id=region_id,
        repo_dir=paths.repo,
        spec_path=raw["spec_path"],
        ledger_dir=paths.ledger_root / commit / region_slug(region_id),
        strategy_path=strategy_path,
        baseline_strategy_path=baseline_strategy_path,
        working_copy_dir=paths.working_copy,
        manifest=code.manifest,
        visible_dataset_dir=visible_dataset_dir,
    )


def load_gateway_config(path, *, seed_if_empty: bool = False) -> GatewayConfig:
    """Read the configuration file and build one RegionConfig per region.

    `seed_if_empty` is for the service's own startup: it lets a first run
    turn the seed directory into the baseline repository. A reader such
    as the ledger CLI leaves it off, so pointing the CLI at a
    configuration whose repository is missing reports that rather than
    quietly creating an empty one.
    """
    path = Path(path)
    where = f"gateway config {path}"
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is not a mapping")

    _check_keys(raw, TOP_LEVEL_KEYS, (), where)
    if raw["version"] != VERSION:
        raise ValueError(f"{where} has version {raw['version']!r}; this reader understands {VERSION}")

    paths = _load_paths(raw["paths"], where)
    if not paths.working_copy.is_dir():
        raise ValueError(f"{where}: working_copy {paths.working_copy} is not a directory")
    if not paths.strategies.is_dir():
        raise ValueError(f"{where}: strategies {paths.strategies} is not a directory")
    if not paths.programs.is_dir():
        raise ValueError(f"{where}: programs {paths.programs} is not a directory")

    commit = _resolve_baseline_commit(paths, where, seed_if_empty)

    codes = {
        name: _load_code(name, code_raw, paths, where)
        for name, code_raw in raw["codes"].items()
    }
    regions = {
        region_id: _load_region(region_id, region_raw, paths, codes, commit, where)
        for region_id, region_raw in raw["regions"].items()
    }
    return GatewayConfig(paths=paths, baseline_commit=commit, codes=codes, regions=regions)
