from pathlib import Path

from equivalent.components import sanitize
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
CASES = {"case0000": {"h_in": "aGk=", "u_in": "aGk="}, "case0001": {"h_in": "eW8=", "u_in": "eW8="}}


def test_all_tools_pass_uses_only_the_first_case():
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    results = sanitize.check("ch04:step", "tree123", strategy, CASES, builder)

    assert set(results) == {"memcheck", "racecheck", "initcheck"}
    assert all(r["verdict"] == "pass" for r in results.values())
    assert list(builder.sanitize_calls[0]["case"]) == ["case0000"]


def test_one_failing_tool_does_not_fail_the_others():
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.sanitize_ok = False

    results = sanitize.check("ch04:step", "tree123", strategy, CASES, builder)

    assert all(r["verdict"] == "fail" for r in results.values())
    assert results["memcheck"]["detail"]["errors"] == 3
