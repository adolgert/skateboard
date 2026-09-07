from pathlib import Path

import pytest
import yaml

from equivalent.components import sanitize
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, fixture_case, write_program

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
CASES = {"case0000": fixture_case(), "case0001": fixture_case(offset=4)}


def _manifest(tmp_path):
    """The code's own description, which is where the replay executable is named."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def _strategy_sanitizing(tmp_path, which_cases):
    """The stdpar_managed strategy with its case selection changed to `which_cases`."""
    d = yaml.safe_load(STRATEGY_PATH.read_text())
    d["sanitize_cases"] = which_cases
    path = tmp_path / f"sanitize-{which_cases}.yaml"
    path.write_text(yaml.safe_dump(d))
    return load_strategy(path)


def test_all_tools_pass_and_the_strategy_chooses_which_cases_run(tmp_path):
    strategy = _strategy_sanitizing(tmp_path, "first")
    builder = FakeBuilder()

    results = sanitize.check("ch04:step", "tree123", strategy, _manifest(tmp_path), CASES, builder)

    assert set(results) == {"memcheck", "racecheck", "initcheck"}
    assert all(r["verdict"] == "pass" for r in results.values())
    assert list(builder.sanitize_calls[0]["cases"]) == ["case0000"]


def test_a_strategy_asking_for_every_case_sends_every_case(tmp_path):
    strategy = _strategy_sanitizing(tmp_path, "all")
    builder = FakeBuilder()

    results = sanitize.check("ch04:step", "tree123", strategy, _manifest(tmp_path), CASES, builder)

    assert all(r["verdict"] == "pass" for r in results.values())
    assert list(builder.sanitize_calls[0]["cases"]) == ["case0000", "case0001"]


def test_one_failing_tool_does_not_fail_the_others(tmp_path):
    strategy = _strategy_sanitizing(tmp_path, "first")
    builder = FakeBuilder()
    builder.sanitize_ok = False

    results = sanitize.check("ch04:step", "tree123", strategy, _manifest(tmp_path), CASES, builder)

    assert all(r["verdict"] == "fail" for r in results.values())
    assert results["memcheck"]["detail"]["errors"] == 3


def test_a_failed_top_level_run_with_no_tool_results_fails_every_requested_tool(tmp_path):
    class Incomplete(FakeBuilder):
        def sanitize(self, *args, **kwargs):
            return {"ok": False, "stage": "sanitize", "per_tool": {},
                    "log_tail": "replay executable is missing"}

    results = sanitize.check(
        "ch04:step", "tree123", _strategy_sanitizing(tmp_path, "first"),
        _manifest(tmp_path), CASES, Incomplete(),
    )

    assert all(row["verdict"] == "fail" for row in results.values())
    assert all("missing" in row["detail"]["reason"] for row in results.values())


def test_an_unavailable_tool_is_a_failure_not_a_vacuous_pass(tmp_path):
    class Unavailable(FakeBuilder):
        def sanitize(self, *args, **kwargs):
            return {
                "ok": False, "stage": "sanitize",
                "per_tool": {
                    "memcheck": {"ok": None, "error": "compute-sanitizer not found"},
                    "racecheck": {"ok": True, "errors": 0, "log_tail": ""},
                    "initcheck": {"ok": True, "errors": 0, "log_tail": ""},
                },
            }

    results = sanitize.check(
        "ch04:step", "tree123", _strategy_sanitizing(tmp_path, "first"),
        _manifest(tmp_path), CASES, Unavailable(),
    )

    assert results["memcheck"]["verdict"] == "fail"
    assert "not found" in results["memcheck"]["detail"]["reason"]
    assert results["racecheck"]["verdict"] == "pass"


def test_a_malformed_tool_result_fails_closed(tmp_path):
    class Malformed(FakeBuilder):
        def sanitize(self, *args, **kwargs):
            return {"ok": True, "stage": "sanitize", "per_tool": {
                "memcheck": {"ok": "yes", "errors": 0},
                "racecheck": {"ok": True, "errors": 0},
                "initcheck": {"ok": True, "errors": 0},
            }}

    results = sanitize.check(
        "ch04:step", "tree123", _strategy_sanitizing(tmp_path, "first"),
        _manifest(tmp_path), CASES, Malformed(),
    )

    assert results["memcheck"]["verdict"] == "fail"


@pytest.mark.parametrize("errors", [None, -1, 1, True, "0"])
def test_a_passing_tool_requires_a_zero_integer_error_count(tmp_path, errors):
    class BadCount(FakeBuilder):
        def sanitize(self, *args, **kwargs):
            response = super().sanitize(*args, **kwargs)
            response["per_tool"]["memcheck"]["errors"] = errors
            return response

    results = sanitize.check(
        "ch04:step", "tree123", _strategy_sanitizing(tmp_path, "first"),
        _manifest(tmp_path), CASES, BadCount(),
    )

    assert results["memcheck"]["verdict"] == "fail"
    assert "error count" in results["memcheck"]["detail"]["reason"]


def test_the_shipped_strategy_sanitizes_the_first_case(tmp_path):
    # What the deployment actually does today, read from the strategy file
    # rather than fixed in the component.
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    sanitize.check("ch04:step", "tree123", strategy, _manifest(tmp_path), CASES, builder)

    assert list(builder.sanitize_calls[0]["cases"]) == ["case0000"]


def test_the_replay_executable_the_manifest_names_is_what_is_sanitized(tmp_path):
    # Not a fixed binary name: another code calls its replay driver
    # something else, and the sanitizer has to be pointed at that.
    strategy = _strategy_sanitizing(tmp_path, "first")
    manifest = _manifest(tmp_path)
    builder = FakeBuilder()

    sanitize.check("ch04:step", "tree123", strategy, manifest, CASES, builder)

    assert builder.sanitize_calls[0]["executable"] == manifest.build.targets["replay"].executable
