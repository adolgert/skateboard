"""Timing a port and timing the baseline it is measured against."""
import pytest

from equivalent.components import timing
from equivalent.components.errors import ComponentError
from equivalent.ledger.capture_sets import load_capture_set, program_variable
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import BASELINE_STRATEGY, strategy as strategy_named
from equivalent.tests.fakes import FakeBuilder, keep_program_set, timing_array, write_program


def _manifest(tmp_path):
    """The code's own description of what to run, with what, for how long."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def _seed(harness):
    """A pristine baseline tree, which is what a baseline timing builds."""
    seed = harness.tmp_path / "seed" / "src"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "mod_kernel.f90").write_text("module mod_kernel\nend module\n")
    return harness.tmp_path / "seed"


def _already_built(harness, flags=("-O2", "-stdpar=gpu"), manifest=None):
    """A tree that has built and whose program has been compared, as a port's timing needs."""
    harness.claim("build/replay", {"flags": list(flags)})
    manifest = manifest or _manifest(harness.tmp_path)
    program_set = keep_program_set(harness.store, {
        program_variable(path): timing_array(path) for path in manifest.timing.outputs
    })
    harness.claim("program/regression", {"program_set": program_set})
    return manifest


def _port(harness, manifest, builder=None, **config):
    harness.repo(_seed(harness))
    return timing.check_port(
        harness.context(region_id="ch04:step", phase="porting", manifest=manifest,
                        builder=builder or harness.builder),
        config,
    )


def _baseline(harness, manifest, builder=None, *, baseline_strategy=None, **config):
    """The baseline timing, with the set it packed filed as the gateway files it."""
    harness.repo(_seed(harness))
    result = timing.check_baseline(
        harness.context(
            region_id="ch04:step", phase="porting", manifest=manifest,
            baseline_strategy=baseline_strategy or strategy_named(BASELINE_STRATEGY),
            builder=builder or harness.builder,
        ),
        config,
    )
    harness.keep(result)
    return result


def test_port_pass_reports_the_measured_runs_and_the_build_claims_flags(harness):
    builder = FakeBuilder()
    _already_built(harness)

    result = _port(harness, _manifest(harness.tmp_path), builder)

    assert result.verdict == "pass"
    assert result.detail["runs_s"] == builder.runs_s
    # The timing claim records the flags the binary was actually built
    # with, read back from the tree's own build/replay claim.
    assert result.detail["flags"] == ["-O2", "-stdpar=gpu"]


def test_the_program_that_is_timed_is_the_one_the_manifest_names(harness):
    builder = FakeBuilder()
    manifest = _manifest(harness.tmp_path)
    _already_built(harness)

    _port(harness, manifest, builder)

    call = builder.time_calls[0]
    assert call["executable"] == manifest.build.targets["timing"].executable
    assert call["args"] == list(manifest.timing.args)
    assert call["env"] == dict(manifest.timing.env)
    assert call["outputs"] == list(manifest.timing.outputs)
    assert call["budget_s"] == manifest.timing.budget_s


def test_the_claim_records_the_arguments_and_environment_the_run_was_given(harness, monkeypatch):
    # Two timing claims that disagree should be tellable apart without
    # going back to whatever the manifest said that day.
    import yaml
    directory = write_program(harness.tmp_path)
    raw = yaml.safe_load((directory / "manifest.yaml").read_text())
    raw["timing"] = {
        "args": ["512", "2000"], "outputs": ["energy.csv"], "budget_s": 120,
        "env": {"OMP_NUM_THREADS": "8"},
    }
    (directory / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    manifest = load_manifest(directory / "manifest.yaml")
    _already_built(harness, manifest=manifest)

    result = _port(harness, manifest, FakeBuilder())

    assert result.detail["args"] == ["512", "2000"]
    assert result.detail["env"] == {"OMP_NUM_THREADS": "8"}
    # The files the run wrote are named and hashed, not carried: they are
    # the program's output, and the claim is evidence about it.
    assert list(result.detail["outputs"]) == ["energy.csv"]
    assert len(result.detail["outputs"]["energy.csv"]) == 64


def test_port_fail_when_the_binary_is_not_built(harness):
    builder = FakeBuilder()
    builder.time_ok = False
    _already_built(harness)

    result = _port(harness, _manifest(harness.tmp_path), builder)

    assert result.verdict == "fail"


def test_a_wrong_intermediate_timed_result_fails_even_if_the_last_run_is_right(harness):
    import base64
    from equivalent.capture import npy

    class WrongSecondRun(FakeBuilder):
        def timing_outputs(self, outputs, run):
            result = super().timing_outputs(outputs, run)
            if run == 1:
                result[outputs[0]] = base64.b64encode(npy.encode(timing_array(outputs[0]) + 1)).decode()
            return result

    _already_built(harness)
    result = _port(harness, _manifest(harness.tmp_path), WrongSecondRun())
    assert result.verdict == "fail"
    assert result.detail["compared_repetitions"] == 5
    assert not result.detail["per_run"][1]["field"]["pass"]
    assert result.detail["per_run"][-1]["field"]["pass"]


@pytest.mark.parametrize("field,value", [("outputs", []), ("runs_s", []),
                                         ("runs_s", [0.0] * 5), ("runs_s", [float("nan")] * 5)])
def test_incomplete_or_invalid_timing_cannot_pass(harness, field, value):
    class Incomplete(FakeBuilder):
        def time(self, *args, **kwargs):
            return {**super().time(*args, **kwargs), field: value}

    _already_built(harness)
    result = _port(harness, _manifest(harness.tmp_path), Incomplete())
    assert result.verdict == "fail"


def test_baseline_cannot_pass_after_ignoring_the_requested_flags(harness):
    class IgnoringFlags(FakeBuilder):
        def build(self, *args, **kwargs):
            return {**super().build(*args, **kwargs), "flags_reached_every_compile": False}

    result = _baseline(harness, _manifest(harness.tmp_path), IgnoringFlags(), baseline_strategy=strategy_named(BASELINE_STRATEGY))
    assert result.verdict == "fail"


def test_a_code_that_declares_no_timing_target_is_an_error_naming_it(harness):
    import yaml
    directory = write_program(harness.tmp_path)
    raw = yaml.safe_load((directory / "manifest.yaml").read_text())
    del raw["build"]["targets"]["timing"]
    (directory / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    manifest = load_manifest(directory / "manifest.yaml")
    _already_built(harness, manifest=manifest)

    with pytest.raises(ComponentError) as excinfo:
        _port(harness, manifest, FakeBuilder())

    assert "timing" in str(excinfo.value)


def test_baseline_builds_the_pristine_tree_with_the_regions_baseline_strategy(harness):
    baseline_strategy = strategy_named(BASELINE_STRATEGY)
    builder = FakeBuilder()

    result = _baseline(harness, _manifest(harness.tmp_path), builder, baseline_strategy=baseline_strategy)

    assert result.verdict == "pass"
    # The comparison floor is a strategy file, so the claim can say which
    # one and with which flags the floor was compiled.
    assert result.detail["strategy"] == "cpu_reference"
    assert builder.build_calls[0]["flags"] == list(
        baseline_strategy.languages["fortran"].flags
    )
    assert builder.time_calls[0]["attempt_id"] == builder.build_calls[0]["attempt_id"]


def test_baseline_fail_when_the_baseline_itself_does_not_build(harness):
    builder = FakeBuilder()
    builder.build_ok = False

    result = _baseline(harness, _manifest(harness.tmp_path), builder, baseline_strategy=strategy_named(BASELINE_STRATEGY))

    assert result.verdict == "fail"
    assert builder.time_calls == []


def test_the_baseline_keeps_what_its_program_wrote_as_the_ports_reference(harness):
    # This is where the reference for a port's own whole-program run comes
    # from: not a file checked in beside the code, but this deployment's
    # own baseline run.
    manifest = _manifest(harness.tmp_path)

    result = _baseline(harness, manifest)

    assert result.verdict == "pass"
    stored = load_capture_set(harness.store, result.detail["program_set"])
    # One case, whose variables are the files the program wrote, named by
    # their paths without the suffix.
    assert list(stored) == ["program"]
    assert sorted(stored["program"]["outputs"]) == ["field", "results/flux"]
    assert (stored["program"]["outputs"]["field"] == timing_array("field.npy")).all()


def test_a_code_that_declares_no_timing_outputs_stores_no_set_and_says_so(harness):
    import yaml
    directory = write_program(harness.tmp_path)
    raw = yaml.safe_load((directory / "manifest.yaml").read_text())
    raw["timing"]["outputs"] = []
    (directory / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))

    result = _baseline(harness, load_manifest(directory / "manifest.yaml"))

    assert result.verdict == "pass"
    assert result.detail["program_set"] is None
    assert "no timing outputs" in result.detail["program_set_absent"]
    assert not list(harness.store.capture_sets_dir.iterdir())
