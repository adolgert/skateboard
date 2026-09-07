"""Replaying the captured inputs and demanding the captured outputs back.

These read as the statement of what "the harness replays" means: the
replay driver, given the inputs the capture program recorded, writes the
outputs the capture program recorded -- bitwise, not within a band. The
tolerance policy is for judging a port; the driver and the capture
program are two halves of one harness, and they either agree exactly or
the harness is not describing the code.
"""
from __future__ import annotations

import base64

from equivalent.capture import npy
from equivalent.components import harness_replay
from equivalent.tree import attempt_id_for_strategy
from equivalent.tests.fakes import FakeBuilder

REGION = "tsunami:onboarding"


def _check(harness, builder):
    """The replay check on a tree whose capture has already passed."""
    harness.captured()
    return harness_replay.check(harness.context(builder=builder), {})


def _replaying_builder():
    builder = FakeBuilder()
    builder.replays_capture = True
    return builder


def test_a_driver_that_reproduces_every_captured_output_passes(harness):
    builder = _replaying_builder()

    result = _check(harness, builder)

    assert result.verdict == "pass"
    datasets = result.detail["datasets"]
    assert sorted(datasets) == ["holdout", "visible"]
    assert datasets["visible"]["cases"] == 2
    assert len(datasets["visible"]["capture_set"]) == 64
    # Every captured case was replayed, both datasets in one run each.
    assert [call["executable"] for call in builder.run_calls] == ["replay", "replay"]
    assert builder.run_calls[0]["attempt_id"] == attempt_id_for_strategy(
        REGION, harness.tree.sha, "cpu_reference",
    )


def test_the_replay_is_given_the_captured_inputs(harness):
    builder = _replaying_builder()

    _check(harness, builder)

    sent = builder.run_calls[0]["cases"]
    assert sorted(sent) == ["case0000", "case0001"]
    assert sorted(sent["case0000"]) == ["field", "flux"]


class DriftingBuilder(FakeBuilder):
    """A driver whose answer is one element off in one case's one variable."""

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        result = super().run(attempt_id, executable, cases, notify, mandatory)
        drifted = npy.decode(base64.b64decode(result["outputs"]["case0001"]["flux"]))
        drifted[0, 0] += 1
        result["outputs"]["case0001"]["flux"] = base64.b64encode(npy.encode(drifted)).decode()
        return result


def test_one_element_out_of_place_fails_naming_the_case_and_the_variable(harness):
    builder = DriftingBuilder()
    builder.replays_capture = True

    result = _check(harness, builder)

    assert result.verdict == "fail"
    difference = result.detail["datasets"]["visible"]["first_difference"]
    assert difference["case"] == "case0001"
    assert difference["variable"] == "flux"
    assert difference["max_abs"] == 1.0


class SilentBuilder(FakeBuilder):
    """A driver that writes no file at all for one declared output."""

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        result = super().run(attempt_id, executable, cases, notify, mandatory)
        del result["outputs"]["case0000"]["field"]
        return result


def test_an_output_the_driver_never_wrote_fails_naming_it(harness):
    builder = SilentBuilder()
    builder.replays_capture = True

    result = _check(harness, builder)

    assert result.verdict == "fail"
    difference = result.detail["datasets"]["visible"]["first_difference"]
    assert difference["variable"] == "field"
    assert "wrote no" in difference["reason"]


def test_a_replay_that_would_not_run_fails_with_what_the_builder_said(harness):
    builder = _replaying_builder()
    builder.run_ok = False

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert "runtime crash" in result.detail["datasets"]["visible"]["log_tail"]


