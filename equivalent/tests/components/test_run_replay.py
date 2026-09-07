import base64
from functools import partial

import numpy as np
import pytest

from equivalent.capture import npy
from equivalent.components import run_replay
from equivalent.gateway.backend_client import RunResponse
from equivalent.components.errors import ComponentError
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import PORT_STRATEGY, strategy as strategy_named
from equivalent.tests.fakes import (
    LAUNCHES,
    FakeBuilder,
    fixture_case,
    replayed,
    write_program,
)


@pytest.fixture
def region(harness):
    """A porting region of the fixture code, with its visible case on disk."""
    code = write_program(harness.tmp_path)
    harness.repo()
    harness.manifest = load_manifest(code / "manifest.yaml")
    harness.visible = code / "datasets" / "visible"
    return harness


def _check(region, *, visible=True):
    return run_replay.check(
        region.context(
            region_id="ch04:step", phase="porting",
            strategy=strategy_named(PORT_STRATEGY), manifest=region.manifest,
            visible_dataset=region.visible if visible else None,
        ),
        {},
    )


def test_pass_with_kernels_launched(region):
    result = _check(region)

    assert result.verdict == "pass"
    assert result.detail["kernels_launched"] == 4
    assert "case0000" in result.detail["outputs"]


def test_fail_when_no_kernels_launched_even_though_the_run_succeeded(region):
    region.builder = FakeBuilder(run=partial(replayed, kernels_launched=0))

    result = _check(region)

    assert result.verdict == "fail"
    assert result.detail["kernels_launched"] == 0


def test_fail_when_the_builder_run_itself_fails(region):
    region.builder = FakeBuilder(run=RunResponse(ok=False, log_tail="runtime crash"))

    result = _check(region)

    assert result.verdict == "fail"


def test_raises_component_error_with_no_visible_dataset(region):
    with pytest.raises(ComponentError):
        _check(region, visible=False)


def test_fail_when_the_replay_wrote_no_output_for_a_declared_variable(region):
    # The builder returns whatever files the driver wrote and knows no
    # better; a driver that dropped a variable has to be caught here.
    absent = region.manifest.interface.outputs[0].name
    region.builder = FakeBuilder(run=partial(
        replayed, writes={k: v for k, v in fixture_case().items() if k != absent},
    ))

    result = _check(region)

    assert result.verdict == "fail"
    assert absent in str(result.detail["outputs_rejected"])


def test_fail_when_an_output_came_back_as_the_wrong_element_type(region):
    wrong = region.manifest.interface.outputs[0]
    region.builder = FakeBuilder(run=partial(replayed, writes={
        **fixture_case(),
        wrong.name: base64.b64encode(npy.encode(np.zeros(4, dtype="<i4"))).decode(),
    }))

    result = _check(region)

    assert result.verdict == "fail"
    assert wrong.name in str(result.detail["outputs_rejected"])


def test_an_output_nobody_declared_does_not_fail_the_run(region):
    # The oracle reports these; a driver leaving a scratch array beside its
    # outputs is not a reason to say the region did not run.
    region.builder = FakeBuilder(run=partial(replayed, writes={
        **fixture_case(),
        "scratch": base64.b64encode(npy.encode(np.zeros(2, dtype="<f4"))).decode(),
    }))

    result = _check(region)

    assert result.verdict == "pass"


def test_the_lines_that_matched_are_recorded_in_the_claim(region):
    # The claim says which source lines launched kernels, not just how
    # many launches there were, so a reviewer can check the count against
    # the region's own code.
    result = _check(region)

    assert result.detail["launches"] == LAUNCHES
