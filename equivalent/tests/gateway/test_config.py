"""Reading the gateway's configuration file, as a deployment would write it."""
from pathlib import Path

import pytest
import yaml

from equivalent.gateway.config import load_gateway_config
from equivalent.gateway.submit import init_baseline_repo, region_slug
from equivalent.tests.fakes import write_program

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT_CONFIG = REPO_ROOT / "deploy" / "gateway.tsunami.yaml"

CONFIG = {
    "version": 1,
    "paths": {},
    "codes": {"tsunami": {"manifest": "tsunami/manifest.yaml"}},
    "regions": {
        "ch04:step": {
            "code": "tsunami",
            "phase": "porting",
            "spec_path": "notes/regions/ch04-step.sese.yaml",
            "strategy": "stdpar_managed",
            "baseline_strategy": "cpu_reference",
            "visible_dataset": "visible",
        },
    },
}


def _tree(tmp_path, config: dict, *, minimal_manifest: bool = False):
    """A directory laid out the way a deployment mounts one, plus the config file naming it."""
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    repo = tmp_path / "repo"
    baseline = init_baseline_repo(repo, seed)

    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "stdpar_managed.yaml").write_text("name: stdpar_managed\n")
    (strategies / "cpu_reference.yaml").write_text("name: cpu_reference\n")
    (tmp_path / "working").mkdir()
    programs = write_program(tmp_path, minimal=minimal_manifest).parent

    config = {**config, "paths": {
        "repo": str(repo),
        "ledger_root": str(tmp_path / "ledger"),
        "working_copy": str(tmp_path / "working"),
        "programs": str(programs),
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
    # What the pristine baseline is built with when a port's speedup is
    # measured -- a strategy file like any other, not a name in the builder.
    assert cfg.baseline_strategy_path == tmp_path / "strategies" / "cpu_reference.yaml"
    assert cfg.working_copy_dir == tmp_path / "working"
    # The dataset lives under the code that owns it, not in a directory
    # shared by every code the deployment holds.
    assert cfg.visible_dataset_dir == tmp_path / "programs" / "tsunami" / "datasets" / "visible"
    # The ledger sits under the baseline commit, then the region id with
    # the colon replaced -- so two baselines never share a ledger.
    assert cfg.ledger_dir == tmp_path / "ledger" / baseline / "ch04-step"
    assert config.baseline_commit == baseline


def test_the_codes_section_loads_each_manifest_and_the_region_carries_its_own(tmp_path):
    path, _ = _tree(tmp_path, CONFIG)

    config = load_gateway_config(path)

    assert list(config.codes) == ["tsunami"]
    code = config.codes["tsunami"]
    assert code.manifest_path == tmp_path / "programs" / "tsunami" / "manifest.yaml"
    assert code.manifest.name == "tsunami"
    # The region does not look its code up again later: it carries the
    # same loaded manifest, so every claim it files names one file.
    assert config.regions["ch04:step"].manifest is code.manifest


def test_a_region_naming_a_code_the_file_does_not_describe_names_it(tmp_path):
    config = {**CONFIG, "regions": {
        "ch04:step": {**CONFIG["regions"]["ch04:step"], "code": "coarseair"},
    }}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "coarseair" in str(excinfo.value)
    assert "ch04:step" in str(excinfo.value)


def test_a_code_whose_manifest_is_not_there_fails_at_startup(tmp_path):
    config = {**CONFIG, "codes": {"tsunami": {"manifest": "tsunami/no-such-manifest.yaml"}}}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "no-such-manifest.yaml" in str(excinfo.value)


def test_two_regions_become_two_region_configs(tmp_path):
    config = {**CONFIG, "regions": {
        **CONFIG["regions"],
        "ch04:diff": {
            "code": "tsunami",
            "phase": "porting",
            "spec_path": "notes/regions/ch04-diff.sese.yaml",
            "strategy": "stdpar_managed",
            "baseline_strategy": "cpu_reference",
        },
    }}
    path, baseline = _tree(tmp_path, config)

    loaded = load_gateway_config(path)

    assert sorted(loaded.regions) == ["ch04:diff", "ch04:step"]
    assert loaded.regions["ch04:diff"].ledger_dir == tmp_path / "ledger" / baseline / "ch04-diff"
    # A region that names no dataset simply has none; the field is optional.
    assert loaded.regions["ch04:diff"].visible_dataset_dir is None


def test_a_region_missing_a_required_field_names_the_field_and_the_region(tmp_path):
    config = {**CONFIG, "regions": {"ch04:step": {
        "code": "tsunami", "phase": "porting",
        "spec_path": "notes/regions/ch04-step.sese.yaml",
        "baseline_strategy": "cpu_reference",
    }}}
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


def test_a_sessions_directory_is_read_when_named_and_is_none_when_not(tmp_path):
    # The gateway never reads this one. It is written down here so that a
    # tool reading a ledger finds the transcripts of the same deployment
    # without being told a second time.
    path, _ = _tree(tmp_path, CONFIG)
    raw = yaml.safe_load(path.read_text())
    raw["paths"]["sessions"] = str(tmp_path / "sessions")
    path.write_text(yaml.safe_dump(raw))

    assert load_gateway_config(path).paths.sessions == tmp_path / "sessions"

    del raw["paths"]["sessions"]
    path.write_text(yaml.safe_dump(raw))

    assert load_gateway_config(path).paths.sessions is None


def test_a_sessions_directory_that_is_not_on_this_machine_is_not_an_error(tmp_path):
    # The gateway runs where the transcripts are not, so a directory that
    # does not resolve here must not stop the file from loading; whoever
    # needs to read it says so then.
    path, _ = _tree(tmp_path, CONFIG)
    raw = yaml.safe_load(path.read_text())
    raw["paths"]["sessions"] = str(tmp_path / "nowhere")
    path.write_text(yaml.safe_dump(raw))

    assert load_gateway_config(path).paths.sessions == tmp_path / "nowhere"


def test_a_baseline_strategy_whose_file_is_not_there_fails_at_startup(tmp_path):
    config = {**CONFIG, "regions": {"ch04:step": {
        **CONFIG["regions"]["ch04:step"], "baseline_strategy": "cpu_absent",
    }}}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "cpu_absent" in str(excinfo.value)
    assert "ch04:step" in str(excinfo.value)


def test_the_deployments_own_config_names_a_checked_in_spec_for_every_porting_region():
    # The deployment file cannot be loaded here -- its paths are the
    # gateway container's mount points. What is worth checking from
    # outside the container is that each region it names has the spec
    # file the walkthrough copies into the working copy, found by the
    # region id with its colon written as a dash.
    config = yaml.safe_load(DEPLOYMENT_CONFIG.read_text())

    assert sorted(config["regions"]) == ["ch04:step", "ch04:step-stencil", "tsunami:onboard"]
    porting = {
        region_id: region for region_id, region in config["regions"].items()
        if region["phase"] == "porting"
    }
    assert sorted(porting) == ["ch04:step", "ch04:step-stencil"]
    for region_id, region in porting.items():
        code = region["code"]
        spec = REPO_ROOT / "programs" / code / "regions" / f"{region_slug(region_id)}.sese.yaml"
        assert spec.is_file(), f"{region_id} has no checked-in spec at {spec}"
        assert yaml.safe_load(spec.read_text())["region"] == region_id
        # The walkthrough lays that file into the working copy at a path it
        # derives from the region id alone, so the two spellings have to
        # agree or the gateway would read a spec nobody wrote.
        assert region["spec_path"] == f"notes/regions/{region_slug(region_id)}.sese.yaml"


def test_the_deployments_onboarding_region_names_nothing_a_port_would_need():
    # A code is being brought in there, so the spec and the dataset a
    # porting region names are what that session produces rather than
    # what it starts from.
    region = yaml.safe_load(DEPLOYMENT_CONFIG.read_text())["regions"]["tsunami:onboard"]

    assert region["phase"] == "onboarding"
    assert region["strategy"] == "onboarding"
    assert "spec_path" not in region and "visible_dataset" not in region


ONBOARDING_REGION = {
    "code": "tsunami",
    "phase": "onboarding",
    "strategy": "stdpar_managed",
    "baseline_strategy": "cpu_reference",
}


def test_an_onboarding_region_loads_on_a_manifest_that_is_still_minimal(tmp_path):
    # The code has not been described yet -- describing it is what the
    # session is for -- and no region of it has been chosen either.
    config = {**CONFIG, "regions": {"tsunami:onboarding": ONBOARDING_REGION}}
    path, _ = _tree(tmp_path, config, minimal_manifest=True)

    cfg = load_gateway_config(path).regions["tsunami:onboarding"]

    assert cfg.phase == "onboarding"
    assert cfg.spec_path is None
    assert cfg.visible_dataset_dir is None
    assert cfg.manifest.complete is False


def test_a_porting_region_on_a_manifest_that_is_still_minimal_names_what_is_absent(tmp_path):
    path, _ = _tree(tmp_path, CONFIG, minimal_manifest=True)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    message = str(excinfo.value)
    assert "ch04:step" in message
    assert "interface" in message and "datasets" in message


def test_a_porting_region_with_no_spec_path_is_refused(tmp_path):
    config = {**CONFIG, "regions": {"ch04:step": {
        key: value for key, value in CONFIG["regions"]["ch04:step"].items()
        if key != "spec_path"
    }}}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "spec_path" in str(excinfo.value)


def test_an_onboarding_region_naming_a_visible_dataset_is_refused(tmp_path):
    # The datasets a code is judged against are what onboarding produces.
    config = {**CONFIG, "regions": {
        "tsunami:onboarding": {**ONBOARDING_REGION, "visible_dataset": "visible"},
    }}
    path, _ = _tree(tmp_path, config, minimal_manifest=True)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "visible_dataset" in str(excinfo.value)


def test_a_phase_this_reader_does_not_know_is_refused_by_name(tmp_path):
    config = {**CONFIG, "regions": {
        "ch04:step": {**CONFIG["regions"]["ch04:step"], "phase": "porting-ish"},
    }}
    path, _ = _tree(tmp_path, config)

    with pytest.raises(ValueError) as excinfo:
        load_gateway_config(path)

    assert "porting-ish" in str(excinfo.value)
