"""Timing the code's own program twice, and keeping what it wrote.

These read as the statement of what "the harness times" means: the
program the manifest names runs twice inside the budget the manifest
declares, writes the files the manifest declares both times, and writes
the same ones both times. What the last run wrote is stored as the
program's own capture set, which is what a port's whole-program run is
later compared against.
"""
from __future__ import annotations

import base64
from functools import partial

import numpy as np

from equivalent.capture import npy
from equivalent.components import harness_timing
from equivalent.components.answers import TimeResponse
from equivalent.components.workspaces import attempt_id_for_strategy
from equivalent.ledger import capture_sets
from equivalent.tests.fakes import (
    FakeBuilder,
    in_tree_manifest,
    run_seconds,
    timed,
    timing_array,
    timing_files,
    write_tree,
)

REGION = "tsunami:onboarding"
DECLARED_OUTPUTS = ["field.npy", "results/flux.npy"]


def _check(harness, builder, manifest=None):
    """The timing check, with the set it packed filed as the gateway files it."""
    harness.repo(write_tree(harness.tmp_path / "seed", manifest))
    result = harness_timing.check(harness.context(region_id=REGION, builder=builder), {})
    harness.keep(result)
    return result


def test_two_runs_that_agree_pass_and_store_what_the_program_wrote(harness):
    builder = harness.builder

    result = _check(harness, builder)

    assert result.verdict == "pass"
    assert result.detail["runs_s"] == run_seconds(harness_timing.REPEATS)
    assert result.detail["gpu_exclusive"] is True
    # What the program wrote, named and hashed, is what a port's own
    # timing claim calls `outputs` too; what the manifest said it would
    # write is a separate line, so the two cannot be read for each other.
    assert result.detail["declared_outputs"] == DECLARED_OUTPUTS
    assert sorted(result.detail["outputs"]) == DECLARED_OUTPUTS
    assert all(len(digest) == 64 for digest in result.detail["outputs"].values())
    # The program's own outputs are a capture set of one case, whose
    # variables are the files the program wrote, named the way a baseline
    # timing claim names its own.
    stored = capture_sets.load_capture_set(harness.store, result.detail["program_set"])
    assert sorted(stored) == ["program"]
    assert sorted(stored["program"]["outputs"]) == ["field", "results/flux"]
    assert np.array_equal(stored["program"]["outputs"]["field"], timing_array("field.npy"))


def test_the_program_is_timed_the_way_the_manifest_says_and_run_twice(harness):
    builder = harness.builder

    _check(harness, builder)

    call = builder.time_calls[0]
    assert call["executable"] == "whole_program"
    assert call["outputs"] == DECLARED_OUTPUTS
    assert call["budget_s"] == 300
    # Twice, because what is being asked is whether the two agree.
    assert call["repeats"] == 2
    assert call["attempt_id"] == attempt_id_for_strategy(REGION, harness.tree.sha, "cpu_reference")


def _drifting_files(paths, run: int) -> dict:
    """A program that writes a different array the second time it runs."""
    written = timing_files(paths, run)
    if run > 0:
        drifted = npy.decode(base64.b64decode(written["field.npy"]))
        drifted[0] += 1
        written["field.npy"] = base64.b64encode(npy.encode(drifted)).decode()
    return written


def test_a_program_that_writes_something_else_the_second_time_fails_naming_the_file(harness):
    result = _check(harness, FakeBuilder(time=partial(timed, files=_drifting_files)))

    assert result.verdict == "fail"
    assert "field.npy" in "\n".join(result.detail["problems"])


def test_a_run_the_builder_refused_fails_with_what_it_said(harness):
    # An exceeded budget or a missing declared output comes back from the
    # builder as a failed run, and the reason is the builder's own words.
    builder = FakeBuilder(time=TimeResponse(ok=False, log_tail="timing binary not built"))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert "timing binary not built" in result.detail["log_tail"]


def _not_arrays(paths, run: int) -> dict:
    """A program whose declared output is not an array at all."""
    return {path: base64.b64encode(b"3.14, 2.71\n").decode() for path in paths}


def test_an_output_that_is_not_an_array_fails_naming_the_file(harness):
    result = _check(harness, FakeBuilder(time=partial(timed, files=_not_arrays)))

    assert result.verdict == "fail"
    assert "field.npy" in "\n".join(result.detail["problems"])


def test_a_code_that_declares_no_timing_target_is_told_so(harness):
    manifest = in_tree_manifest()
    manifest["build"] = {
        **manifest["build"],
        "targets": {
            role: spec for role, spec in manifest["build"]["targets"].items() if role != "timing"
        },
    }

    result = _check(harness, FakeBuilder(), manifest)

    assert result.verdict == "fail"
    assert "timing" in result.detail["problems"][0]
