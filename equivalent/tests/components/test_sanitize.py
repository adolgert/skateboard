from functools import partial

import pytest
import yaml

from equivalent.components import sanitize
from equivalent.components.errors import ComponentError
from equivalent.gateway.backend_client import SanitizeResponse
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.components.conftest import (
    PORT_STRATEGY,
    STRATEGY_DIR,
    write_visible_dataset,
)
from equivalent.tests.fakes import FakeBuilder, sanitized, write_program

STRATEGY_PATH = STRATEGY_DIR / f"{PORT_STRATEGY}.yaml"
# Two cases, so a strategy that asks for the first is telling the check
# something a strategy that asks for all of them is not.
CASE_OFFSETS = (0, 4)


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


def _check(harness, strategy, *, builder=None):
    harness.repo()
    return sanitize.check(
        harness.context(
            region_id="ch04:step", phase="porting", strategy=strategy,
            manifest=_manifest(harness.tmp_path),
            visible_dataset=write_visible_dataset(harness.tmp_path / "visible", CASE_OFFSETS),
            builder=builder or harness.builder,
        ),
        {},
    )


def test_all_tools_pass_and_the_strategy_chooses_which_cases_run(harness, tmp_path):
    strategy = _strategy_sanitizing(harness.tmp_path, "first")
    builder = harness.builder

    results = _check(harness, strategy, builder=builder)

    assert set(results) == {"sanitize/memcheck", "sanitize/racecheck", "sanitize/initcheck"}
    assert all(r.verdict == "pass" for r in results.values())
    assert list(builder.sanitize_calls[0]["cases"]) == ["case0000"]


def test_a_strategy_asking_for_every_case_sends_every_case(harness, tmp_path):
    strategy = _strategy_sanitizing(harness.tmp_path, "all")
    builder = harness.builder

    results = _check(harness, strategy, builder=builder)

    assert all(r.verdict == "pass" for r in results.values())
    assert list(builder.sanitize_calls[0]["cases"]) == ["case0000", "case0001"]


def test_one_failing_tool_does_not_fail_the_others(harness, tmp_path):
    strategy = _strategy_sanitizing(harness.tmp_path, "first")
    builder = FakeBuilder(sanitize=partial(sanitized, ok=False))

    results = _check(harness, strategy, builder=builder)

    assert all(r.verdict == "fail" for r in results.values())
    assert results["sanitize/memcheck"].detail["errors"] == 3


def test_a_failed_top_level_run_with_no_tool_results_fails_every_requested_tool(harness, tmp_path):
    incomplete = FakeBuilder(sanitize=SanitizeResponse(
        ok=False, log_tail="replay executable is missing",
    ))

    results = _check(harness, _strategy_sanitizing(harness.tmp_path, "first"), builder=incomplete)

    assert all(row.verdict == "fail" for row in results.values())
    assert all("missing" in row.detail["reason"] for row in results.values())


def test_an_unavailable_tool_is_a_failure_not_a_vacuous_pass(harness, tmp_path):
    unavailable = FakeBuilder(sanitize=SanitizeResponse(ok=False, per_tool={
        "memcheck": {"ok": None, "error": "compute-sanitizer not found"},
        "racecheck": {"ok": True, "errors": 0, "log_tail": ""},
        "initcheck": {"ok": True, "errors": 0, "log_tail": ""},
    }))

    results = _check(harness, _strategy_sanitizing(harness.tmp_path, "first"), builder=unavailable)

    assert results["sanitize/memcheck"].verdict == "fail"
    assert "not found" in results["sanitize/memcheck"].detail["reason"]
    assert results["sanitize/racecheck"].verdict == "pass"


def test_a_malformed_tool_result_fails_closed(harness, tmp_path):
    malformed = FakeBuilder(sanitize=SanitizeResponse(ok=True, per_tool={
        "memcheck": {"ok": "yes", "errors": 0},
        "racecheck": {"ok": True, "errors": 0},
        "initcheck": {"ok": True, "errors": 0},
    }))

    results = _check(harness, _strategy_sanitizing(harness.tmp_path, "first"), builder=malformed)

    assert results["sanitize/memcheck"].verdict == "fail"


@pytest.mark.parametrize("errors", [None, 1])
def test_a_passing_tool_requires_a_zero_error_count(harness, errors):
    counted = FakeBuilder(sanitize=partial(sanitized, per_tool={
        "memcheck": {"ok": True, "errors": errors},
        "racecheck": {"ok": True, "errors": 0},
        "initcheck": {"ok": True, "errors": 0},
    }))

    results = _check(harness, _strategy_sanitizing(harness.tmp_path, "first"), builder=counted)

    assert results["sanitize/memcheck"].verdict == "fail"
    assert "error count" in results["sanitize/memcheck"].detail["reason"]


@pytest.mark.parametrize("errors", [-1, True, "0"])
def test_an_error_count_that_is_not_a_count_is_an_error_not_a_verdict(harness, errors):
    # A sanitizer answer nobody can read says nothing about the port, so
    # it must not become a claim saying the port failed one.
    unreadable = FakeBuilder(sanitize=partial(sanitized, per_tool={
        "memcheck": {"ok": True, "errors": errors},
    }))

    with pytest.raises(ComponentError):
        _check(harness, _strategy_sanitizing(harness.tmp_path, "first"), builder=unreadable)


def test_the_shipped_strategy_sanitizes_the_first_case(harness, tmp_path):
    # What the deployment actually does today, read from the strategy file
    # rather than fixed in the component.
    strategy = load_strategy(STRATEGY_PATH)
    builder = harness.builder

    _check(harness, strategy, builder=builder)

    assert list(builder.sanitize_calls[0]["cases"]) == ["case0000"]


def test_the_replay_executable_the_manifest_names_is_what_is_sanitized(harness, tmp_path):
    # Not a fixed binary name: another code calls its replay driver
    # something else, and the sanitizer has to be pointed at that.
    strategy = _strategy_sanitizing(harness.tmp_path, "first")
    manifest = _manifest(harness.tmp_path)
    builder = harness.builder

    _check(harness, strategy, builder=builder)

    assert builder.sanitize_calls[0]["executable"] == manifest.build.targets["replay"].executable
