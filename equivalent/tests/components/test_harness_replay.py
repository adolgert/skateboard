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
from dataclasses import replace

from equivalent.capture import npy
from equivalent.components import harness_replay
from equivalent.components.answers import RunResponse
from equivalent.components.workspaces import attempt_id_for_strategy
from equivalent.tests.fakes import FakeBuilder, replayed

REGION = "tsunami:onboarding"


def _check(harness, builder):
    """The replay check on a tree whose capture has already passed."""
    harness.captured()
    return harness_replay.check(harness.context(builder=builder), {})


def test_a_driver_that_reproduces_every_captured_output_passes(harness):
    builder = FakeBuilder()

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
    builder = FakeBuilder()

    _check(harness, builder)

    sent = builder.run_calls[0]["cases"]
    assert sorted(sent) == ["case0000", "case0001"]
    assert sorted(sent["case0000"]) == ["field", "flux"]


def _drifting(request):
    """A driver whose answer is one element off in one case's one variable."""
    resp = replayed(request)
    drifted = npy.decode(base64.b64decode(resp.outputs["case0001"]["flux"]))
    drifted[0, 0] += 1
    case = {**resp.outputs["case0001"],
            "flux": base64.b64encode(npy.encode(drifted)).decode()}
    return replace(resp, outputs={**resp.outputs, "case0001": case})


def test_one_element_out_of_place_fails_naming_the_case_and_the_variable(harness):
    builder = FakeBuilder(run=_drifting)

    result = _check(harness, builder)

    assert result.verdict == "fail"
    difference = result.detail["datasets"]["visible"]["first_difference"]
    assert difference["case"] == "case0001"
    assert difference["variable"] == "flux"
    assert difference["max_abs"] == 1.0


def _silent(request):
    """A driver that writes no file at all for one declared output."""
    resp = replayed(request)
    case = {k: v for k, v in resp.outputs["case0000"].items() if k != "field"}
    return replace(resp, outputs={**resp.outputs, "case0000": case})


def test_an_output_the_driver_never_wrote_fails_naming_it(harness):
    builder = FakeBuilder(run=_silent)

    result = _check(harness, builder)

    assert result.verdict == "fail"
    difference = result.detail["datasets"]["visible"]["first_difference"]
    assert difference["variable"] == "field"
    assert "wrote no" in difference["reason"]


def test_a_replay_that_would_not_run_fails_with_what_the_builder_said(harness):
    builder = FakeBuilder(run=RunResponse(ok=False, log_tail="runtime crash"))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert "runtime crash" in result.detail["datasets"]["visible"]["log_tail"]


