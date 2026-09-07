"""The one place a whole-program run is asked for and read.

Timing the baseline of a promoted code and timing the program of a code
being brought in are two different questions about the same run, and each
was once asked with its own call and its own idea of what counts as a
measurement. These say that both go through the same call now, and that
the set of files they leave behind is named the same way in both claims --
so a reader, and program_regression, find it without knowing which check
wrote it.
"""
from __future__ import annotations

from functools import partial

import pytest

from equivalent.components import harness_timing, program_outputs, timing
from equivalent.components.names import PROGRAM_SET_KEY
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import (
    BASELINE_STRATEGY,
    Harness,
    strategy as strategy_named,
)
from equivalent.tests.fakes import FakeBuilder, timed, write_program, write_tree


def _time_baseline(harness):
    """The porting check that times the pristine baseline and keeps its files."""
    manifest = load_manifest(write_program(harness.tmp_path) / "manifest.yaml")
    harness.pristine()
    result = timing.check_baseline(
        harness.porting(
            region_id="ch04:step", manifest=manifest,
            baseline_strategy=strategy_named(BASELINE_STRATEGY), builder=FakeBuilder(),
        ),
        {},
    )
    harness.keep(result)
    return result


def _time_harness(tmp_path, builder=None):
    """The onboarding check that times the tree's own program twice.

    In its own region: a baseline timing builds a pristine tree that
    carries no manifest, and an onboarding one is judged by the manifest
    inside its tree.
    """
    harness = Harness(tmp_path)
    harness.repo(write_tree(harness.tmp_path / "seed"))
    result = harness_timing.check(
        harness.context(builder=builder or FakeBuilder()), {},
    )
    harness.keep(result)
    return result


def test_both_phases_time_the_program_through_the_one_shared_call(harness, tmp_path, monkeypatch):
    asked = []

    def record(ctx, attempt_id, manifest, repeats, described=None):
        asked.append(ctx.phase)
        raise AssertionError("stop here: what is being asked is who called")

    monkeypatch.setattr(program_outputs, "time_program", record)

    with pytest.raises(AssertionError):
        _time_baseline(harness)
    with pytest.raises(AssertionError):
        _time_harness(tmp_path / "onboarding")

    assert asked == ["porting", "onboarding"]


def test_the_stored_program_set_is_named_the_same_in_both_claims(harness, tmp_path):
    baseline = _time_baseline(harness)

    assert baseline.verdict == "pass"
    assert baseline.detail[PROGRAM_SET_KEY]

    times = _time_harness(tmp_path / "onboarding")

    assert times.verdict == "pass"
    assert times.detail[PROGRAM_SET_KEY]
    assert PROGRAM_SET_KEY == "program_set"


def test_a_builder_answer_that_is_not_a_measurement_fails_in_both_phases(harness, tmp_path):
    def short_of_repetitions():
        """A builder that answers with fewer runs than it was asked for."""
        return FakeBuilder(time=partial(timed, repetitions=1))

    manifest = load_manifest(write_program(harness.tmp_path) / "manifest.yaml")
    harness.pristine()
    baseline = timing.check_baseline(
        harness.porting(
            region_id="ch04:step", manifest=manifest,
            baseline_strategy=strategy_named(BASELINE_STRATEGY),
            builder=short_of_repetitions(),
        ),
        {"repeats": 3},
    )

    times = _time_harness(tmp_path / "onboarding", short_of_repetitions())

    assert baseline.verdict == "fail"
    assert times.verdict == "fail"
