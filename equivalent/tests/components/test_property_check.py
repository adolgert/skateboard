"""What the property check asks the builder for, and what it records.

The builder is a fake here: running a code's own property module needs a
built replay binary and an installed Hypothesis, neither of which exists
in this development environment. What is being checked is the component's
side of the contract -- the module it names, the seed it draws or is
given, and what the claim carries afterwards.
"""
from functools import partial

import pytest

from equivalent.components import property_check
from equivalent.components.errors import ComponentError
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import PORT_STRATEGY, strategy as strategy_named
from equivalent.tests.fakes import (
    PROPERTIES_IN_TREE,
    FakeBuilder,
    fixture_case,
    property_run,
    write_program,
)

REGION = "ch04:step"


def _check(harness, *, properties=True, visible=True, builder=None, **config):
    """The property check as the gateway calls it, on the fixture code."""
    code = write_program(harness.tmp_path, properties=properties)
    harness.repo()
    return property_check.check(
        harness.context(
            region_id=REGION, phase="porting", strategy=strategy_named(PORT_STRATEGY),
            manifest=load_manifest(code / "manifest.yaml"),
            visible_dataset=(code / "datasets" / "visible") if visible else None,
            builder=builder or harness.builder,
        ),
        config,
    )


def _cases():
    return {"case0000": fixture_case()}


def test_a_passing_property_run_is_a_pass_naming_the_module_and_the_counts(harness):
    builder = harness.builder

    result = _check(harness, seed=1234, max_examples=25)

    assert result.verdict == "pass"
    assert result.detail["module"] == PROPERTIES_IN_TREE
    assert result.detail["passed"] == 3
    assert result.detail["failed"] == 0
    assert result.detail["replays_observed"] == 3

    call = builder.properties_calls[0]
    assert call["module"] == PROPERTIES_IN_TREE
    assert call["executable"] == "replay"
    assert call["cases"] == _cases()


def test_a_failing_property_is_a_fail_carrying_the_falsifying_example(harness):
    # Hypothesis prints the minimized example into pytest's own output, so
    # what the claim has to keep is that output.
    builder = FakeBuilder(properties=partial(
        property_run, ok=False, passed=2, failed=1, collected=3, executed=3,
        log_tail="Falsifying example: test_mass_is_conserved(shift=1)\n1 failed, 2 passed",
    ))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert result.detail["failed"] == 1
    assert "Falsifying example" in result.detail["log_tail"]


@pytest.mark.parametrize("counts", [
    {"passed": 0, "skipped": 1, "collected": 1, "executed": 0},
    {"passed": 0, "collected": 0, "executed": 0},
])
def test_no_executed_passing_property_can_never_be_a_pass(harness, counts):
    builder = FakeBuilder(properties=partial(property_run, ok=False, **counts))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert "no property test passed" in result.detail["problems"]


def test_inconsistent_success_and_failure_counts_fail_closed(harness):
    builder = FakeBuilder(properties=partial(
        property_run, ok=True, passed=2, failed=1, collected=3, executed=3,
    ))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert any("inconsistent" in problem for problem in result.detail["problems"])


def test_backend_must_echo_the_requested_property_configuration(harness):
    wrong = FakeBuilder(properties=lambda request: property_run(
        request, seed=request["seed"] + 1,
    ))

    result = _check(harness, builder=wrong, seed=4, max_examples=10)

    assert result.verdict == "fail"
    assert any("seed" in problem for problem in result.detail["problems"])


@pytest.mark.parametrize("observed", [None, 0])
def test_a_pass_requires_a_protected_observation_of_the_replay_executable(harness, observed):
    builder = FakeBuilder(properties=partial(property_run, replays_observed=observed))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert any("replay" in problem for problem in result.detail["problems"])


@pytest.mark.parametrize("observed", [-1, True, "3"])
def test_an_observation_count_that_is_not_a_count_is_an_error_not_a_verdict(harness, observed):
    # A number nobody can read is not a search that failed; it is an
    # answer the harness cannot use, and no claim may be filed from it.
    builder = FakeBuilder(properties=partial(property_run, replays_observed=observed))

    with pytest.raises(ComponentError):
        _check(harness, builder=builder)


@pytest.mark.parametrize("status", ["xfailed", "xpassed"])
def test_an_expected_failure_or_unexpected_pass_cannot_hide_in_a_property_pass(harness, status):
    builder = FakeBuilder(properties=partial(
        property_run, ok=False, passed=1, collected=2, executed=2, **{status: 1},
    ))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert any(status in problem for problem in result.detail["problems"])


@pytest.mark.parametrize("max_examples", [0, -1])
def test_a_nonpositive_example_count_is_rejected_before_execution(harness, max_examples):
    builder = harness.builder

    with pytest.raises(ComponentError):
        _check(harness, max_examples=max_examples)

    assert builder.properties_calls == []


def test_the_seed_a_person_gave_is_the_seed_that_runs_and_the_seed_recorded(harness):
    # The point of naming a seed is to run again exactly what failed, so
    # the number in the claim and the number the builder was given have to
    # be the one the request asked for.
    builder = harness.builder

    result = _check(harness, seed=987654321)

    assert result.detail["seed"] == 987654321
    assert builder.properties_calls[0]["seed"] == 987654321


def test_a_request_that_names_no_seed_draws_one_and_records_it(harness):
    builder = harness.builder

    first = _check(harness)
    second = _check(harness)

    assert first.detail["seed"] == builder.properties_calls[0]["seed"]
    assert second.detail["seed"] == builder.properties_calls[1]["seed"]
    # Two runs that were told nothing explore somewhere new.
    assert first.detail["seed"] != second.detail["seed"]


def test_how_many_examples_defaults_and_can_be_asked_for(harness):
    builder = harness.builder

    _check(harness)
    _check(harness, max_examples=7)

    assert builder.properties_calls[0]["max_examples"] == property_check.DEFAULT_MAX_EXAMPLES
    assert builder.properties_calls[1]["max_examples"] == 7


def test_a_code_that_declares_no_property_module_is_an_error_not_a_verdict(harness):
    # There is nothing to run, so there is nothing to be right or wrong
    # about; a fail here would read as "this code's invariants broke".
    builder = harness.builder

    with pytest.raises(ComponentError) as excinfo:
        _check(harness, properties=False)

    assert "no properties module" in str(excinfo.value)
    assert builder.properties_calls == []


def test_a_region_with_no_visible_dataset_is_an_error(harness):
    with pytest.raises(ComponentError):
        _check(harness, visible=False)


def test_a_builder_that_cannot_be_reached_is_an_error_not_a_failed_property(harness):
    unreachable = FakeBuilder(properties=OSError("connection refused"))

    with pytest.raises(ComponentError) as excinfo:
        _check(harness, builder=unreachable)

    assert "connection refused" in str(excinfo.value)
