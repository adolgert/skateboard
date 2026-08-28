"""Reading the gateway's configuration file, as a deployment would write it."""
import pytest
import yaml

from equivalent.gateway.config import load_gateway_config
from equivalent.gateway.submit import init_baseline_repo

CONFIG = {
    "version": 1,
    "paths": {},
    "regions": {
        "ch04:step": {
            "spec_path": "notes/regions/ch04-step.sese.yaml",
            "strategy": "stdpar_managed",
            "visible_dataset": "visible",
        },
    },
}


def _tree(tmp_path, config: dict):
    """A directory laid out the way a deployment mounts one, plus the config file naming it."""
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    repo = tmp_path / "repo"
    baseline = init_baseline_repo(repo, seed)

    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "stdpar_managed.yaml").write_text("name: stdpar_managed\n")
    (tmp_path / "working").mkdir()
    (tmp_path / "datasets" / "visible").mkdir(parents=True)

    config = {**config, "paths": {
        "repo": str(repo),
        "ledger_root": str(tmp_path / "ledger"),
        "working_copy": str(tmp_path / "working"),
        "datasets_root": str(tmp_path / "datasets"),
        "strategies": str(strategies),
        "seed": str(seed),
        **config["paths"],
    }}
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(config))
    return path, baseline


def test_one_region_becomes_one_region_config_with_every_path_joined(tmp_path):
    path, baseline = _tree(tmp_path, CONFIG)

    config = load_gateway_config(path)

    assert list(config.regions) == ["ch04:step"]
    cfg = config.regions["ch04:step"]
    assert cfg.repo_dir == tmp_path / "repo"
    assert cfg.spec_path == "notes/regions/ch04-step.sese.yaml"
    assert cfg.strategy_path == tmp_path / "strategies" / "stdpar_managed.yaml"
    assert cfg.working_copy_dir == tmp_path / "working"
    assert cfg.visible_dataset_dir == tmp_path / "datasets" / "visible"
    # The ledger sits under the baseline commit, then the region id with
    # the colon replaced -- so two baselines never share a ledger.
    assert cfg.ledger_dir == tmp_path / "ledger" / baseline / "ch04-step"
    assert config.baseline_commit == baseline


def test_two_regions_become_two_region_configs(tmp_path):
    config = {**CONFIG, "regions": {
        **CONFIG["regions"],
        "ch04:diff": {"spec_path": "notes/regions/ch04-diff.sese.yaml", "strategy": "stdpar_managed"},
    }}
    path, baseline = _tree(tmp_path, config)

    loaded = load_gateway_config(path)

    assert sorted(loaded.regions) == ["ch04:diff", "ch04:step"]
    assert loaded.regions["ch04:diff"].ledger_dir == tmp_path / "ledger" / baseline / "ch04-diff"
    # A region that names no dataset simply has none; the field is optional.
    assert loaded.regions["ch04:diff"].visible_dataset_dir is None


def test_a_region_missing_a_required_field_names_the_field_and_the_region(tmp_path):
    config = {**CONFIG, "regions": {"ch04:step": {"spec_path": "notes/regions/ch04-step.sese.yaml"}}}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "strategy" in str(excinfo.value)
    assert "ch04:step" in str(excinfo.value)


def test_an_unknown_key_names_the_key(tmp_path):
    config = {**CONFIG, "regions": {"ch04:step": {**CONFIG["regions"]["ch04:step"], "stratgey": "typo"}}}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "stratgey" in str(excinfo.value)
    assert "ch04:step" in str(excinfo.value)


def test_a_missing_paths_field_names_the_field(tmp_path):
    path, _ = _tree(tmp_path, CONFIG)
    raw = yaml.safe_load(path.read_text())
    del raw["paths"]["ledger_root"]
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "ledger_root" in str(excinfo.value)


def test_a_strategy_whose_file_is_not_there_fails_when_the_file_is_read(tmp_path):
    # Not at the first request that would have used it: a deployment
    # pointed at a strategy that isn't mounted should not start.
    config = {**CONFIG, "regions": {"ch04:step": {**CONFIG["regions"]["ch04:step"], "strategy": "omp_target"}}}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "omp_target" in str(excinfo.value)


def test_a_version_this_reader_does_not_understand_is_an_error(tmp_path):
    path, _ = _tree(tmp_path, {**CONFIG, "version": 2})

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "version" in str(excinfo.value)


def test_an_uninitialized_repository_is_seeded_only_when_the_caller_asks(tmp_path):
    path, _ = _tree(tmp_path, CONFIG)
    raw = yaml.safe_load(path.read_text())
    raw["paths"]["repo"] = str(tmp_path / "fresh-repo")
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)
    assert "fresh-repo" in str(excinfo.value)

    config = load_gateway_config(path, seed_if_empty=True)
    assert len(config.baseline_commit) == 40
