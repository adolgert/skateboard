import pytest

from equivalent.components import regression
from equivalent.components.errors import ComponentError
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import PORT_STRATEGY, strategy as strategy_named
from equivalent.components.answers import RunResponse
from equivalent.tests.fakes import FakeBuilder, FakeOracle, fixture_case, write_program


def _porting(harness):
    """A porting region of the fixture code, ready for a comparison."""
    harness.repo()
    return harness.context(
        region_id="ch04:step", phase="porting", strategy=strategy_named(PORT_STRATEGY),
        manifest=load_manifest(write_program(harness.tmp_path) / "manifest.yaml"),
    )


def test_visible_reads_outputs_from_the_stored_gpu_executed_claim_not_a_new_run(harness):
    outputs = {"case0000": fixture_case()}
    harness.claim("gpu/executed", {"kernels_launched": 4, "outputs": outputs})

    result = regression.check_visible(_porting(harness), {})

    assert result.verdict == "pass"
    assert harness.oracle.compare_calls[0]["outputs"] == outputs


def test_visible_is_an_error_when_the_run_claim_recorded_no_outputs(harness):
    # The precondition table is what puts a passing run claim in the
    # context, so the only thing left to be wrong is a claim that passed
    # and kept nothing to compare.
    harness.claim("gpu/executed", {"kernels_launched": 4})

    with pytest.raises(ComponentError):
        regression.check_visible(_porting(harness), {})


def test_holdout_never_puts_outputs_or_per_case_detail_in_its_own_claim(harness):
    result = regression.check_holdout(_porting(harness), {})

    assert result.verdict == "pass"
    assert "outputs" not in result.detail
    assert "per_case" not in result.detail


def test_holdout_fetches_inputs_from_the_oracle_and_runs_them_through_the_builder(harness):
    regression.check_holdout(_porting(harness), {})

    assert list(harness.builder.run_calls[0]["cases"]) == ["hcase0"]
    assert harness.builder.run_calls[0]["profile"] is True
    assert harness.oracle.compare_calls[0]["dataset"] == "holdout"


@pytest.mark.parametrize("transport", [False, True])
def test_failed_holdout_does_not_echo_candidate_output(harness, transport):
    harness.builder = FakeBuilder(run=(
        RuntimeError("SECRET_HELD_OUT_INPUT") if transport
        else RunResponse(ok=False, log_tail="SECRET_HELD_OUT_INPUT")
    ))

    with pytest.raises(ComponentError) as raised:
        regression.check_holdout(_porting(harness), {})

    assert "SECRET" not in str(raised.value)
    assert "withheld" in str(raised.value)


def test_the_tolerance_policy_a_verdict_was_reached_under_is_a_material(harness):
    # Which bands judged a comparison is part of what the verdict rests
    # on, so the check declares it rather than leaving it a note.
    harness.claim("gpu/executed", {"outputs": {"case0000": fixture_case()}})

    result = regression.check_visible(_porting(harness), {})

    assert [(s.kind, s.sha256) for s in result.materials] == [
        ("policy", FakeOracle().policy().policy_sha256),
    ]
