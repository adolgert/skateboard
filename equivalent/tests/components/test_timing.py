from pathlib import Path

import pytest

from equivalent.components import timing
from equivalent.components.errors import ComponentError
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.capture_sets import capture_sets_dir, load_capture_set
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, timing_array, write_program

TREE = Subject(kind="tree", sha256="1" * 64)
BASELINE_STRATEGY_PATH = (
    Path(__file__).resolve().parents[2] / "strategy" / "files" / "cpu_reference.yaml"
)


def _manifest(tmp_path):
    """The code's own description of what to run, with what, for how long."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def _store_with_build_claim(tmp_path, flags=("-O2", "-stdpar=gpu")):
    store = LedgerStore(tmp_path / "region")
    store.record_claim(
        [TREE], "build/replay",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass",
                  detail={"flags": list(flags)}),
        [], "sess-1",
    )
    return store


def _baseline_repo(tmp_path):
    seed = tmp_path / "seed" / "src"
    seed.mkdir(parents=True)
    (seed / "mod_kernel.f90").write_text("module mod_kernel\nend module\n")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, tmp_path / "seed")
    return repo_dir


def test_port_pass_reports_the_measured_runs_and_the_build_claims_flags(tmp_path):
    builder = FakeBuilder()
    store = _store_with_build_claim(tmp_path)

    result = timing.check_port(store, TREE, "ch04:step", "tree123", _manifest(tmp_path), builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["runs_s"] == builder.runs_s
    # The timing claim records the flags the binary was actually built
    # with, read back from the tree's own build/replay claim.
    assert result["detail"]["flags"] == ["-O2", "-stdpar=gpu"]


def test_the_program_that_is_timed_is_the_one_the_manifest_names(tmp_path):
    builder = FakeBuilder()
    manifest = _manifest(tmp_path)
    store = _store_with_build_claim(tmp_path)

    timing.check_port(store, TREE, "ch04:step", "tree123", manifest, builder)

    call = builder.time_calls[0]
    assert call["executable"] == manifest.build.targets["timing"].executable
    assert call["args"] == list(manifest.timing.args)
    assert call["env"] == dict(manifest.timing.env)
    assert call["outputs"] == list(manifest.timing.outputs)
    assert call["budget_s"] == manifest.timing.budget_s


def test_the_claim_records_the_arguments_and_environment_the_run_was_given(tmp_path, monkeypatch):
    # Two timing claims that disagree should be tellable apart without
    # going back to whatever the manifest said that day.
    import yaml
    directory = write_program(tmp_path)
    raw = yaml.safe_load((directory / "manifest.yaml").read_text())
    raw["timing"] = {
        "args": ["512", "2000"], "outputs": ["energy.csv"], "budget_s": 120,
        "env": {"OMP_NUM_THREADS": "8"},
    }
    (directory / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    manifest = load_manifest(directory / "manifest.yaml")
    store = _store_with_build_claim(tmp_path)

    result = timing.check_port(store, TREE, "ch04:step", "tree123", manifest, FakeBuilder())

    assert result["detail"]["args"] == ["512", "2000"]
    assert result["detail"]["env"] == {"OMP_NUM_THREADS": "8"}
    # The files the run wrote are named and hashed, not carried: they are
    # the program's output, and the claim is evidence about it.
    assert list(result["detail"]["outputs"]) == ["energy.csv"]
    assert len(result["detail"]["outputs"]["energy.csv"]) == 64


def test_port_fail_when_the_binary_is_not_built(tmp_path):
    builder = FakeBuilder()
    builder.time_ok = False
    store = _store_with_build_claim(tmp_path)

    result = timing.check_port(store, TREE, "ch04:step", "tree123", _manifest(tmp_path), builder)

    assert result["verdict"] == "fail"


def test_port_refuses_to_time_a_tree_with_no_passing_build_claim(tmp_path):
    store = LedgerStore(tmp_path / "region")

    with pytest.raises(ComponentError):
        timing.check_port(store, TREE, "ch04:step", "tree123", _manifest(tmp_path), FakeBuilder())


def test_a_code_that_declares_no_timing_target_is_an_error_naming_it(tmp_path):
    import yaml
    directory = write_program(tmp_path)
    raw = yaml.safe_load((directory / "manifest.yaml").read_text())
    del raw["build"]["targets"]["timing"]
    (directory / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    store = _store_with_build_claim(tmp_path)

    with pytest.raises(ComponentError) as excinfo:
        timing.check_port(
            store, TREE, "ch04:step", "tree123",
            load_manifest(directory / "manifest.yaml"), FakeBuilder(),
        )

    assert "timing" in str(excinfo.value)


def test_baseline_builds_the_pristine_tree_with_the_regions_baseline_strategy(tmp_path):
    repo_dir = _baseline_repo(tmp_path)
    baseline_strategy = load_strategy(BASELINE_STRATEGY_PATH)
    builder = FakeBuilder()

    result = timing.check_baseline(
        LedgerStore(tmp_path / "region"), repo_dir, "ch04:step", "basetree123",
        _manifest(tmp_path), baseline_strategy, builder,
    )

    assert result["verdict"] == "pass"
    # The comparison floor is a strategy file, so the claim can say which
    # one and with which flags the floor was compiled.
    assert result["detail"]["strategy"] == "cpu_reference"
    assert builder.build_calls[0]["flags"] == list(
        baseline_strategy.languages["fortran"].flags
    )
    assert builder.time_calls[0]["attempt_id"] == builder.build_calls[0]["attempt_id"]


def test_baseline_fail_when_the_baseline_itself_does_not_build(tmp_path):
    repo_dir = _baseline_repo(tmp_path)
    builder = FakeBuilder()
    builder.build_ok = False

    result = timing.check_baseline(
        LedgerStore(tmp_path / "region"), repo_dir, "ch04:step", "basetree123",
        _manifest(tmp_path), load_strategy(BASELINE_STRATEGY_PATH), builder,
    )

    assert result["verdict"] == "fail"
    assert builder.time_calls == []


def test_the_baseline_keeps_what_its_program_wrote_as_the_ports_reference(tmp_path):
    # This is where the reference for a port's own whole-program run comes
    # from: not a file checked in beside the code, but this deployment's
    # own baseline run.
    repo_dir = _baseline_repo(tmp_path)
    store = LedgerStore(tmp_path / "region")
    manifest = _manifest(tmp_path)

    result = timing.check_baseline(
        store, repo_dir, "ch04:step", "basetree123", manifest,
        load_strategy(BASELINE_STRATEGY_PATH), FakeBuilder(),
    )

    assert result["verdict"] == "pass"
    stored = load_capture_set(store, result["detail"]["program_set"])
    # One case, whose variables are the files the program wrote, named by
    # their paths without the suffix.
    assert list(stored) == ["program"]
    assert sorted(stored["program"]["outputs"]) == ["field", "results/flux"]
    assert (stored["program"]["outputs"]["field"] == timing_array("field.npy")).all()


def test_a_code_that_declares_no_timing_outputs_stores_no_set_and_says_so(tmp_path):
    import yaml
    directory = write_program(tmp_path)
    raw = yaml.safe_load((directory / "manifest.yaml").read_text())
    raw["timing"]["outputs"] = []
    (directory / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    store = LedgerStore(tmp_path / "region")

    result = timing.check_baseline(
        store, _baseline_repo(tmp_path), "ch04:step", "basetree123",
        load_manifest(directory / "manifest.yaml"),
        load_strategy(BASELINE_STRATEGY_PATH), FakeBuilder(),
    )

    assert result["verdict"] == "pass"
    assert result["detail"]["program_set"] is None
    assert "no timing outputs" in result["detail"]["program_set_absent"]
    assert not list(capture_sets_dir(store).iterdir())
