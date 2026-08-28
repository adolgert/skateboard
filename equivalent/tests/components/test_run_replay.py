import base64
from pathlib import Path

import numpy as np
import pytest

# kernel_launches comes straight from the file the builder image runs, so
# this test fails if the two drift apart. It reads a string and needs no
# GPU, no compiler, and no container.
from services.builder.stages import kernel_launches
from equivalent.capture import npy
from equivalent.components import run_replay
from equivalent.components.errors import ComponentError
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, fixture_case, write_program

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
CASES = {"case0000": fixture_case()}


@pytest.fixture
def manifest(tmp_path):
    """The fixture code's own description of what its region reads and writes."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def test_pass_with_kernels_launched(manifest):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["kernels_launched"] == 4
    assert "case0000" in result["detail"]["outputs"]


def test_fail_when_no_kernels_launched_even_though_the_run_succeeded(manifest):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.run_kernels = 0

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["kernels_launched"] == 0


def test_fail_when_the_builder_run_itself_fails(manifest):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.run_ok = False

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["verdict"] == "fail"


def test_raises_component_error_with_no_visible_dataset(manifest):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    with pytest.raises(ComponentError):
        run_replay.check("ch04:step", "tree123", strategy, manifest, {}, builder)


def test_fail_when_the_replay_wrote_no_output_for_a_declared_variable(manifest):
    # The builder returns whatever files the driver wrote and knows no
    # better; a driver that dropped a variable has to be caught here.
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    absent = manifest.interface.outputs[0].name
    builder.run_outputs = {k: v for k, v in fixture_case().items() if k != absent}

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["verdict"] == "fail"
    assert absent in str(result["detail"]["outputs_rejected"])


def test_fail_when_an_output_came_back_as_the_wrong_element_type(manifest):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    wrong = manifest.interface.outputs[0]
    builder.run_outputs = {
        **fixture_case(),
        wrong.name: base64.b64encode(npy.encode(np.zeros(4, dtype="<i4"))).decode(),
    }

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["verdict"] == "fail"
    assert wrong.name in str(result["detail"]["outputs_rejected"])


def test_an_output_nobody_declared_does_not_fail_the_run(manifest):
    # The oracle reports these; a driver leaving a scratch array beside its
    # outputs is not a reason to say the region did not run.
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.run_outputs = {
        **fixture_case(),
        "scratch": base64.b64encode(npy.encode(np.zeros(2, dtype="<f4"))).decode(),
    }

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["verdict"] == "pass"


# Two real lines the NVIDIA runtime prints under NVCOMPILER_ACC_NOTIFY=1:
# the first from a -stdpar/OpenACC build, the second from -mp=gpu. They
# differ in their later fields and in the run of spaces after "kernel".
ACC_LINE = (
    "launch CUDA kernel  file=/tmp/probe/p.f90 function=p line=5 device=0 "
    "threadid=1 num_gangs=782 num_workers=1 vector_length=128 grid=782 block=128"
)
OMP_LINE = (
    "launch CUDA kernel file=/tmp/probe/q.f90 function=q line=5 device=0 "
    "host-threadid=0 num_teams=0 thread_limit=0 kernelname=nvkernel_MAIN__F1L5_2_ "
    "grid=<<<782,1,1>>> block=<<<128,1,1>>> shmem=0b"
)


def test_both_real_notify_lines_are_counted_and_located():
    count, launches = kernel_launches(f"{ACC_LINE}\n{OMP_LINE}\n", "acc")

    assert count == 2
    assert launches == [
        ("/tmp/probe/p.f90", "p", "5"),
        ("/tmp/probe/q.f90", "q", "5"),
    ]


def test_a_line_the_program_printed_itself_is_not_a_launch():
    # The device proof is only worth anything if a program cannot pass it
    # by printing the words. Without the runtime's own file/function/line/
    # device fields, nothing is counted.
    forged = "launch CUDA kernel  forged line\nlaunch CUDA kernel\nlaunching kernels now\n"

    assert kernel_launches(forged, "acc") == (0, [])


def test_nothing_is_counted_for_a_strategy_that_asks_for_no_notify_output():
    assert kernel_launches(ACC_LINE, None) == (0, [])


def test_the_lines_that_matched_are_recorded_in_the_claim(manifest):
    # The claim says which source lines launched kernels, not just how
    # many launches there were, so a reviewer can check the count against
    # the region's own code.
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    result = run_replay.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert result["detail"]["launches"] == builder.run_launches
