from pathlib import Path

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
