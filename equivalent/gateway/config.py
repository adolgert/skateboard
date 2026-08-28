"""The gateway's configuration file: one YAML naming every path and region.

Trust role: this decides which repository, which ledger, which working
copy, and which strategy each region is checked against. A wrong entry
here does not make a check lie, but it makes every claim describe
something other than what the person thinks they are reviewing. So the
file is read strictly: an unknown key, a missing field, or a strategy
file that is not on disk stops the gateway at startup rather than
surfacing as a puzzling failure on the first request.

The file has two sections::

    version: 1
    paths:
      repo: /repo
      ledger_root: /ledger
      working_copy: /working
      datasets_root: /datasets
      strategies: /strategies
      seed: /seed
    regions:
      "ch04:step":
        spec_path: notes/regions/ch04-step.sese.yaml
        strategy: stdpar_managed
        visible_dataset: visible

Each region's directories are built by joining the two: the ledger lives
at `<ledger_root>/<baseline commit>/<region id with ':' replaced by
'-'>`, the strategy at `<strategies>/<strategy>.yaml`, and the visible
dataset at `<datasets_root>/<visible_dataset>`. Nothing spells those
layouts a second time.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import baseline_commit, init_baseline_repo, region_slug

VERSION = 1

TOP_LEVEL_KEYS = ("version", "paths", "regions")
REQUIRED_PATH_KEYS = ("repo", "ledger_root", "working_copy", "strategies")
OPTIONAL_PATH_KEYS = ("datasets_root", "seed")
REQUIRED_REGION_KEYS = ("spec_path", "strategy")
OPTIONAL_REGION_KEYS = ("visible_dataset",)


@dataclass(frozen=True)
class Paths:
    """The directories the gateway works in, as the file named them."""

    repo: Path
    ledger_root: Path
    working_copy: Path
    strategies: Path
    datasets_root: Path | None = None
    seed: Path | None = None


@dataclass(frozen=True)
class GatewayConfig:
    paths: Paths
    baseline_commit: str
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


def _load_region(
    region_id: str, raw: dict, paths: Paths, commit: str, where: str
) -> RegionConfig:
    region_where = f"{where} region '{region_id}'"
    _check_keys(raw, REQUIRED_REGION_KEYS, OPTIONAL_REGION_KEYS, region_where)

    strategy_path = paths.strategies / f"{raw['strategy']}.yaml"
    if not strategy_path.is_file():
        raise ValueError(
            f"{region_where} names strategy '{raw['strategy']}', but {strategy_path} does not exist"
        )

    visible_dataset_dir = None
    if raw.get("visible_dataset") is not None:
        if paths.datasets_root is None:
            raise ValueError(
                f"{region_where} names visible_dataset '{raw['visible_dataset']}', but "
                f"paths has no datasets_root to look it up in"
            )
        visible_dataset_dir = paths.datasets_root / raw["visible_dataset"]
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
        working_copy_dir=paths.working_copy,
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

    commit = _resolve_baseline_commit(paths, where, seed_if_empty)

    regions = {
        region_id: _load_region(region_id, region_raw, paths, commit, where)
        for region_id, region_raw in raw["regions"].items()
    }
    return GatewayConfig(paths=paths, baseline_commit=commit, regions=regions)
