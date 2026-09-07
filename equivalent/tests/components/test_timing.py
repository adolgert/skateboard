"""Timing a port and timing the baseline it is measured against."""
from functools import partial

import pytest

from equivalent.components import timing
from equivalent.components.errors import ComponentError
from equivalent.components.answers import TimeResponse
from equivalent.components.program_outputs import program_variable
from equivalent.ledger.capture_sets import load_capture_set
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import BASELINE_STRATEGY, strategy as strategy_named
from equivalent.tests.fakes import (
    FakeBuilder,
    built,
    keep_program_set,
    run_seconds,
    timed,
    timing_array,
    timing_files,
    write_program,
)


def _manifest(tmp_path):
    """The code's own description of what to run, with what, for how long."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


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
    harness.pristine()
    return timing.check_port(
        harness.context(region_id="ch04:step", phase="porting", manifest=manifest,
                        builder=builder or harness.builder),
        config,
    )


def _baseline(harness, manifest, builder=None, *, baseline_strategy=None, **config):
    """The baseline timing, with the set it packed filed as the gateway files it."""
    harness.pristine()
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
    assert result.detail["runs_s"] == run_seconds(timing.DEFAULT_REPEATS)
    # The timing claim records the flags the binary was actually built
    # with, read back from the tree's own build/replay claim.
    assert result.detail["flags"] == ["-O2", "-stdpar=gpu"]


def test_the_port_timing_names_what_its_comparison_rested_on(harness):
    # The claim's detail says which baseline outputs and which bands the
    # repetitions were compared under; the same two are the materials, so
    # a verdict reached under one policy cannot read as a verdict under
    # another.
    manifest = _manifest(harness.tmp_path)
    _already_built(harness, manifest=manifest)

    result = _port(harness, manifest, FakeBuilder())

    assert result.verdict == "pass"
    assert [material.kind for material in result.materials] == ["policy", "capture_set"]
    named = {material.kind: material.sha256 for material in result.materials}
    assert named["policy"] == result.detail["policy_sha256"]
    assert named["capture_set"] == result.detail["program_set"]


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
    builder = FakeBuilder(time=TimeResponse(ok=False, log_tail="timing binary not built"))
    _already_built(harness)

    result = _port(harness, _manifest(harness.tmp_path), builder)

    assert result.verdict == "fail"


def test_a_wrong_intermediate_timed_result_fails_even_if_the_last_run_is_right(harness):
    import base64
    from equivalent.capture import npy

    def wrong_second_run(paths, run):
        written = timing_files(paths, run)
        if run == 1:
            written[paths[0]] = base64.b64encode(
                npy.encode(timing_array(paths[0]) + 1)
            ).decode()
        return written

    _already_built(harness)
    result = _port(harness, _manifest(harness.tmp_path),
                   FakeBuilder(time=partial(timed, files=wrong_second_run)))
    assert result.verdict == "fail"
    assert result.detail["compared_repetitions"] == 5
    assert not result.detail["per_run"][1]["field"]["pass"]
    assert result.detail["per_run"][-1]["field"]["pass"]


@pytest.mark.parametrize("field,value", [("outputs", []), ("runs_s", []),
                                         ("runs_s", [0.0] * 5), ("runs_s", [float("nan")] * 5)])
def test_incomplete_or_invalid_timing_cannot_pass(harness, field, value):
    incomplete = FakeBuilder(time=partial(timed, **{field: value}))

    _already_built(harness)
    result = _port(harness, _manifest(harness.tmp_path), incomplete)
    assert result.verdict == "fail"


def test_baseline_cannot_pass_after_ignoring_the_requested_flags(harness):
    ignoring_flags = FakeBuilder(build=partial(built, flags_reached_every_compile=False))

    result = _baseline(harness, _manifest(harness.tmp_path), ignoring_flags, baseline_strategy=strategy_named(BASELINE_STRATEGY))
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
    builder = FakeBuilder(build=partial(built, ok=False))

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


def _performance_claims(harness, manifest, *, baseline_runs, port_runs,
                        gpu_exclusive=True):
    baseline = _baseline(
        harness, manifest,
        FakeBuilder(time=partial(
            timed, runs_s=baseline_runs, gpu_exclusive=gpu_exclusive,
        )),
    )
    assert baseline.verdict == "pass"
    harness.claim("timing/baseline", baseline.detail)
    _already_built(harness, manifest=manifest)
    port = _port(
        harness, manifest, FakeBuilder(time=partial(
            timed, runs_s=port_runs, gpu_exclusive=gpu_exclusive,
        )),
    )
    harness.claim("timing/port", port.detail)


def test_performance_passes_only_when_the_recorded_median_beats_the_manifest_floor(harness):
    manifest = _manifest(harness.tmp_path)
    _performance_claims(
        harness, manifest, baseline_runs=[0.30] * 5, port_runs=[0.20] * 5,
        gpu_exclusive=False,
    )

    result = timing.check_performance(harness.porting(manifest=manifest), {})

    assert result.verdict == "pass"
    assert result.detail["median_speedup"] == pytest.approx(1.5)
    assert result.detail["min_median_speedup"] == 1.10
    assert result.detail["measurement_scope"] == timing.MEASUREMENT_SCOPE
    assert result.detail["gpu_exclusivity_required"] is False
    assert [material.kind for material in result.materials] == ["timing_claim", "timing_claim"]
    assert result.detail["baseline_claim_id"] == harness.claims["timing/baseline"].id
    assert result.detail["port_claim_id"] == harness.claims["timing/port"].id


def test_performance_failure_keeps_the_observed_medians_for_review(harness):
    manifest = _manifest(harness.tmp_path)
    _performance_claims(
        harness, manifest, baseline_runs=[0.20] * 5, port_runs=[0.20] * 5,
    )

    result = timing.check_performance(harness.porting(manifest=manifest), {})

    assert result.verdict == "fail"
    assert "median speedup" in result.reasons[0]


def test_performance_refuses_an_overflowed_ratio_without_writing_non_json_numbers(harness):
    manifest = _manifest(harness.tmp_path)
    _performance_claims(
        harness, manifest, baseline_runs=[1e308] * 5, port_runs=[1e-308] * 5,
    )

    result = timing.check_performance(harness.porting(manifest=manifest), {})

    assert result.verdict == "fail"
    assert result.detail["median_speedup"] is None
    assert "finite speedup" in result.reasons[0]


def test_performance_requires_five_samples_in_each_existing_timing_claim(harness):
    manifest = _manifest(harness.tmp_path)
    _performance_claims(
        harness, manifest, baseline_runs=[0.30] * 5, port_runs=[0.20] * 4,
    )

    with pytest.raises(ComponentError, match="fewer than 5 samples"):
        timing.check_performance(harness.porting(manifest=manifest), {})
