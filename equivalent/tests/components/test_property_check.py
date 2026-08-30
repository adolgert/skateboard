"""What the property check asks the builder for, and what it records.

The builder is a fake here: running a code's own property module needs a
built replay binary and an installed Hypothesis, neither of which exists
in this development environment. What is being checked is the component's
side of the contract -- the module it names, the seed it draws or is
given, and what the claim carries afterwards.
"""
import pytest

from equivalent.components import property_check
from equivalent.components.errors import ComponentError
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import (
    PROPERTIES_IN_TREE,
    FakeBuilder,
    fixture_case,
    write_program,
)

REGION = "ch04:step"
TREE = "a" * 64


def _manifest(tmp_path, *, properties=True):
    return load_manifest(
        write_program(tmp_path, properties=properties) / "manifest.yaml"
    )


def _cases():
    return {"case0000": fixture_case()}


def test_a_passing_property_run_is_a_pass_naming_the_module_and_the_counts(tmp_path):
    builder = FakeBuilder()
    builder.properties_counts = {"passed": 3, "failed": 0, "errors": 0}

    result = property_check.check(
        REGION, TREE, _manifest(tmp_path), _cases(), builder, seed=1234, max_examples=25,
    )

    assert result["verdict"] == "pass"
    assert result["detail"]["module"] == PROPERTIES_IN_TREE
    assert result["detail"]["passed"] == 3
    assert result["detail"]["failed"] == 0

    call = builder.properties_calls[0]
    assert call["module"] == PROPERTIES_IN_TREE
    assert call["executable"] == "replay"
    assert call["cases"] == _cases()


def test_a_failing_property_is_a_fail_carrying_the_falsifying_example(tmp_path):
    # Hypothesis prints the minimized example into pytest's own output, so
    # what the claim has to keep is that output.
    builder = FakeBuilder()
    builder.properties_ok = False
    builder.properties_counts = {"passed": 2, "failed": 1, "errors": 0}
    builder.properties_log = "Falsifying example: test_mass_is_conserved(shift=1)\n1 failed, 2 passed"

    result = property_check.check(REGION, TREE, _manifest(tmp_path), _cases(), builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["failed"] == 1
    assert "Falsifying example" in result["detail"]["log_tail"]


def test_the_seed_a_person_gave_is_the_seed_that_runs_and_the_seed_recorded(tmp_path):
    # The point of naming a seed is to run again exactly what failed, so
    # the number in the claim and the number the builder was given have to
    # be the one the request asked for.
    builder = FakeBuilder()

    result = property_check.check(
        REGION, TREE, _manifest(tmp_path), _cases(), builder, seed=987654321,
    )

    assert result["detail"]["seed"] == 987654321
    assert builder.properties_calls[0]["seed"] == 987654321


def test_a_request_that_names_no_seed_draws_one_and_records_it(tmp_path):
    builder = FakeBuilder()

    first = property_check.check(REGION, TREE, _manifest(tmp_path), _cases(), builder)
    second = property_check.check(REGION, TREE, _manifest(tmp_path), _cases(), builder)

    assert first["detail"]["seed"] == builder.properties_calls[0]["seed"]
    assert second["detail"]["seed"] == builder.properties_calls[1]["seed"]
    # Two runs that were told nothing explore somewhere new.
    assert first["detail"]["seed"] != second["detail"]["seed"]


def test_how_many_examples_defaults_and_can_be_asked_for(tmp_path):
    builder = FakeBuilder()

    property_check.check(REGION, TREE, _manifest(tmp_path), _cases(), builder)
    property_check.check(REGION, TREE, _manifest(tmp_path), _cases(), builder, max_examples=7)

    assert builder.properties_calls[0]["max_examples"] == property_check.DEFAULT_MAX_EXAMPLES
    assert builder.properties_calls[1]["max_examples"] == 7


def test_a_code_that_declares_no_property_module_is_an_error_not_a_verdict(tmp_path):
    # There is nothing to run, so there is nothing to be right or wrong
    # about; a fail here would read as "this code's invariants broke".
    builder = FakeBuilder()

    with pytest.raises(ComponentError) as excinfo:
        property_check.check(
            REGION, TREE, _manifest(tmp_path, properties=False), _cases(), builder,
        )

    assert "no properties module" in str(excinfo.value)
    assert builder.properties_calls == []


def test_a_region_with_no_visible_dataset_is_an_error(tmp_path):
    builder = FakeBuilder()

    with pytest.raises(ComponentError):
        property_check.check(REGION, TREE, _manifest(tmp_path), {}, builder)


def test_a_builder_that_cannot_be_reached_is_an_error_not_a_failed_property(tmp_path):
    class Unreachable(FakeBuilder):
        def properties(self, *args, **kwargs):
            raise OSError("connection refused")

    with pytest.raises(ComponentError) as excinfo:
        property_check.check(REGION, TREE, _manifest(tmp_path), _cases(), Unreachable())

    assert "connection refused" in str(excinfo.value)
