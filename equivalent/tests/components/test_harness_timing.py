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
from pathlib import Path

import numpy as np

from equivalent.capture import npy
from equivalent.components import harness_timing
from equivalent.gateway.submit import attempt_id_for_strategy, init_baseline_repo
from equivalent.ledger import capture_sets
from equivalent.ledger.store import LedgerStore
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, in_tree_manifest, timing_array, write_tree

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
REGION = "tsunami:onboarding"
TREE_SHA = "a" * 64
DECLARED_OUTPUTS = ["field.npy", "results/flux.npy"]


def _repo(tmp_path, manifest=None):
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed", manifest))
    return repo


def _check(tmp_path, builder, manifest=None):
    store = LedgerStore(tmp_path / "ledger")
    return harness_timing.check(
        store, _repo(tmp_path, manifest), "main", REGION, TREE_SHA,
        load_strategy(STRATEGY_DIR / "cpu_reference.yaml"), builder,
    ), store


def test_two_runs_that_agree_pass_and_store_what_the_program_wrote(tmp_path):
    builder = FakeBuilder()

    result, store = _check(tmp_path, builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["runs_s"] == builder.runs_s
    assert result["detail"]["gpu_exclusive"] is True
    assert result["detail"]["outputs"] == DECLARED_OUTPUTS
    # The program's own outputs are a capture set of one case, whose
    # variables are the files the program wrote.
    program = result["detail"]["datasets"]["program"]
    stored = capture_sets.load_capture_set(store, program["capture_set"])
    assert sorted(stored) == ["program"]
    assert sorted(stored["program"]["outputs"]) == ["field", "results/flux"]
    assert np.array_equal(stored["program"]["outputs"]["field"], timing_array("field.npy"))


def test_the_program_is_timed_the_way_the_manifest_says_and_run_twice(tmp_path):
    builder = FakeBuilder()

    _check(tmp_path, builder)

    call = builder.time_calls[0]
    assert call["executable"] == "whole_program"
    assert call["outputs"] == DECLARED_OUTPUTS
    assert call["budget_s"] == 300
    # Twice, because what is being asked is whether the two agree.
    assert call["repeats"] == 2
    assert call["attempt_id"] == attempt_id_for_strategy(REGION, TREE_SHA, "cpu_reference")


class DriftingTimer(FakeBuilder):
    """A program that writes a different array the second time it runs."""

    def timing_outputs(self, outputs, run: int) -> dict:
        written = super().timing_outputs(outputs, run)
        if run > 0:
            drifted = npy.decode(base64.b64decode(written["field.npy"]))
            drifted[0] += 1
            written["field.npy"] = base64.b64encode(npy.encode(drifted)).decode()
        return written


def test_a_program_that_writes_something_else_the_second_time_fails_naming_the_file(tmp_path):
    result, _ = _check(tmp_path, DriftingTimer())

    assert result["verdict"] == "fail"
    assert "field.npy" in "\n".join(result["detail"]["problems"])


def test_a_run_the_builder_refused_fails_with_what_it_said(tmp_path):
    # An exceeded budget or a missing declared output comes back from the
    # builder as a failed run, and the reason is the builder's own words.
    builder = FakeBuilder()
    builder.time_ok = False

    result, _ = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert "timing binary not built" in result["detail"]["log_tail"]


class TimerWritingSomethingElse(FakeBuilder):
    """A program whose declared output is not an array at all."""

    def timing_outputs(self, outputs, run: int) -> dict:
        return {name: base64.b64encode(b"3.14, 2.71\n").decode() for name in outputs}


def test_an_output_that_is_not_an_array_fails_naming_the_file(tmp_path):
    result, _ = _check(tmp_path, TimerWritingSomethingElse())

    assert result["verdict"] == "fail"
    assert "field.npy" in "\n".join(result["detail"]["problems"])


def test_a_code_that_declares_no_timing_target_is_told_so(tmp_path):
    manifest = in_tree_manifest()
    manifest["build"] = {
        **manifest["build"],
        "targets": {
            role: spec for role, spec in manifest["build"]["targets"].items() if role != "timing"
        },
    }

    result, _ = _check(tmp_path, FakeBuilder(), manifest)

    assert result["verdict"] == "fail"
    assert "timing" in result["detail"]["problems"][0]
