"""Capturing and replaying a second time, and demanding the same answers.

These read as the statement of what "the harness is deterministic"
means: the capture program run again with the same arguments writes the
set that is already stored, and the replay driver run twice on the same
inputs writes the same outputs twice. A harness that drifts makes every
claim above it a claim about one particular afternoon.
"""
from __future__ import annotations

import base64


from equivalent.capture import npy
from equivalent.components import harness_determinism
from equivalent.tests.fakes import FakeBuilder, captured_cases

REGION = "tsunami:onboarding"


def _check(harness, builder):
    """The determinism check on a tree whose capture has already passed."""
    harness.captured()
    return harness_determinism.check(harness.context(builder=builder), {})


def _replaying_builder():
    builder = FakeBuilder()
    builder.replays_capture = True
    return builder


def test_capturing_and_replaying_again_agreeing_is_a_pass(harness):
    builder = _replaying_builder()

    result = _check(harness, builder)

    assert result.verdict == "pass"
    datasets = result.detail["datasets"]
    assert datasets["visible"]["recaptured"] == datasets["visible"]["capture_set"]
    assert result.detail["replay"]["same"] is True
    assert result.detail["differed"] == []


def test_the_second_capture_is_a_run_of_its_own(harness):
    # Writing over the first run's output directory would make a program
    # that appends look deterministic.
    builder = _replaying_builder()

    _check(harness, builder)

    assert sorted(call["run_name"] for call in builder.capture_calls) == [
        "holdout-again", "visible-again",
    ]


def test_the_replay_is_run_twice_on_the_visible_inputs(harness):
    builder = _replaying_builder()

    _check(harness, builder)

    assert len(builder.run_calls) == 2
    assert builder.run_calls[0]["cases"] == builder.run_calls[1]["cases"]


class DriftingCaptureBuilder(FakeBuilder):
    """A capture program that writes something else the second time around."""

    def capture(self, attempt_id, executable, args, run_name):
        result = super().capture(attempt_id, executable, args, run_name)
        if run_name.endswith("-again"):
            result["cases"] = captured_cases([*args, "drifted"])
        return result


def test_a_capture_that_does_not_repeat_fails_naming_the_dataset(harness):
    builder = DriftingCaptureBuilder()
    builder.replays_capture = True

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["datasets"]["visible"]["same"] is False
    assert "visible" in "\n".join(result.detail["differed"])


class DriftingReplayBuilder(FakeBuilder):
    """A driver whose second answer is one element off from its first."""

    def __init__(self):
        super().__init__()
        self.replays_capture = True
        self.replays = 0

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        result = super().run(attempt_id, executable, cases, notify, mandatory)
        self.replays += 1
        if self.replays > 1:
            drifted = npy.decode(base64.b64decode(result["outputs"]["case0000"]["field"]))
            drifted[0] += 1
            result["outputs"]["case0000"]["field"] = base64.b64encode(
                npy.encode(drifted)
            ).decode()
        return result


def test_a_replay_that_does_not_repeat_fails_naming_the_case_and_variable(harness):
    result = _check(harness, DriftingReplayBuilder())

    assert result.verdict == "fail"
    difference = result.detail["replay"]["first_difference"]
    assert difference["case"] == "case0000"
    assert difference["variable"] == "field"
    assert "replay" in "\n".join(result.detail["differed"])


def test_a_replay_that_would_not_run_fails_with_what_the_builder_said(harness):
    builder = _replaying_builder()
    builder.run_ok = False

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert "runtime crash" in result.detail["replay"]["log_tail"]


