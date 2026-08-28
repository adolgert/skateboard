from pathlib import Path

import pytest

from equivalent.components import run_replay
from equivalent.components.errors import ComponentError
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
CASES = {"case0000": {"h_in": "aGk=", "u_in": "aGk="}}


def test_pass_with_kernels_launched():
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    result = run_replay.check("ch04:step", "tree123", strategy, CASES, builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["kernels_launched"] == 4
    assert "case0000" in result["detail"]["outputs"]


def test_fail_when_no_kernels_launched_even_though_the_run_succeeded():
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.run_kernels = 0

    result = run_replay.check("ch04:step", "tree123", strategy, CASES, builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["kernels_launched"] == 0


def test_fail_when_the_builder_run_itself_fails():
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.run_ok = False

    result = run_replay.check("ch04:step", "tree123", strategy, CASES, builder)

    assert result["verdict"] == "fail"


def test_raises_component_error_with_no_visible_dataset():
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    with pytest.raises(ComponentError):
        run_replay.check("ch04:step", "tree123", strategy, {}, builder)
