"""Capturing and replaying a second time, and demanding the same answers.

These read as the statement of what "the harness is deterministic"
means: the capture program run again with the same arguments writes the
set that is already stored, and the replay driver run twice on the same
inputs writes the same outputs twice. A harness that drifts makes every
claim above it a claim about one particular afternoon.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from itertools import count

from equivalent.capture import npy
from equivalent.components import harness_determinism
from equivalent.gateway.backend_client import RunResponse
from equivalent.tests.fakes import FakeBuilder, captured, captured_cases, replayed

REGION = "tsunami:onboarding"


def _check(harness, builder):
    """The determinism check on a tree whose capture has already passed."""
    harness.captured()
    return harness_determinism.check(harness.context(builder=builder), {})


def test_capturing_and_replaying_again_agreeing_is_a_pass(harness):
    builder = FakeBuilder()

    result = _check(harness, builder)

    assert result.verdict == "pass"
    datasets = result.detail["datasets"]
    assert datasets["visible"]["recaptured"] == datasets["visible"]["capture_set"]
    assert result.detail["replay"]["same"] is True
    assert result.detail["differed"] == []


def test_the_second_capture_is_a_run_of_its_own(harness):
    # Writing over the first run's output directory would make a program
    # that appends look deterministic.
    builder = FakeBuilder()

    _check(harness, builder)

    assert sorted(call["run_name"] for call in builder.capture_calls) == [
        "holdout-again", "visible-again",
    ]


def test_the_replay_is_run_twice_on_the_visible_inputs(harness):
    builder = FakeBuilder()

    _check(harness, builder)

    assert len(builder.run_calls) == 2
    assert builder.run_calls[0]["cases"] == builder.run_calls[1]["cases"]


def _drifting_capture(request):
    """A capture program that writes something else the second time around."""
    if request["run_name"].endswith("-again"):
        return captured(request, cases=captured_cases([*request["args"], "drifted"]))
    return captured(request)


def test_a_capture_that_does_not_repeat_fails_naming_the_dataset(harness):
    builder = FakeBuilder(capture=_drifting_capture)

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["datasets"]["visible"]["same"] is False
    assert "visible" in "\n".join(result.detail["differed"])


def _drifting_replay():
    """A driver whose second answer is one element off from its first."""
    replays = count()

    def answer(request):
        resp = replayed(request)
        if next(replays) == 0:
            return resp
        drifted = npy.decode(base64.b64decode(resp.outputs["case0000"]["field"]))
        drifted[0] += 1
        case = {**resp.outputs["case0000"],
                "field": base64.b64encode(npy.encode(drifted)).decode()}
        return replace(resp, outputs={**resp.outputs, "case0000": case})

    return answer


def test_a_replay_that_does_not_repeat_fails_naming_the_case_and_variable(harness):
    result = _check(harness, FakeBuilder(run=_drifting_replay()))

    assert result.verdict == "fail"
    difference = result.detail["replay"]["first_difference"]
    assert difference["case"] == "case0000"
    assert difference["variable"] == "field"
    assert "replay" in "\n".join(result.detail["differed"])


def test_a_replay_that_would_not_run_fails_with_what_the_builder_said(harness):
    builder = FakeBuilder(run=RunResponse(ok=False, log_tail="runtime crash"))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert "runtime crash" in result.detail["replay"]["log_tail"]


