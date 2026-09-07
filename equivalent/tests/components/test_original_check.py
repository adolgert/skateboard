"""Onboarding must agree with a reference it did not create itself."""
import pytest

from equivalent.components import original_check
from equivalent.tests.fakes import reference, reference_builder


def check(harness, builder, path):
    """The original comparison, with what it kept filed as the gateway files it."""
    harness.repo()
    result = original_check.check(
        harness.context(region_id="new:onboard", builder=builder, original_reference_path=path),
        {},
    )
    harness.keep(result)
    return result


def test_independent_original_comparison_records_outputs(harness):
    result = check(harness, reference_builder(), reference(harness.tmp_path))
    assert result.verdict == "pass"
    compared = result.detail["runs"][0]["outputs"][0]
    assert compared["original_artifacts"] == compared["candidate_artifacts"]
    assert result.detail["reference_sha256"]


def test_self_consistent_but_wrong_onboarded_program_fails(harness):
    result = check(harness, reference_builder(wrong_candidate=True), reference(harness.tmp_path))
    assert result.verdict == "fail"
    comparison = result.detail["runs"][0]["outputs"][0]
    assert comparison["deterministic"] is True
    assert comparison["comparison_result"]["pass"] is False


def test_a_comparison_that_did_not_agree_keeps_nothing(harness):
    # The claim that keeps both programs' outputs is the claim that says
    # they agreed. A failing one leaves no bytes in the ledger for a
    # reader to mistake for a comparison that was made.
    result = check(harness, reference_builder(wrong_candidate=True),
                   reference(harness.tmp_path))

    assert result.verdict == "fail"
    assert result.stores == ()


@pytest.mark.parametrize("failure", ["drift", "incomplete"])
def test_nonrepeatable_or_incomplete_reference_fails(harness, failure):
    builder = reference_builder(**{failure: True})
    assert check(harness, builder, reference(harness.tmp_path)).verdict == "fail"


def test_no_reference_cannot_establish_onboarding(harness):
    result = check(harness, reference_builder(), None)
    assert result.verdict == "fail"
    assert "original_reference" in result.detail["problems"][0]


def test_nan_cannot_establish_original_agreement(harness):
    # Both programs wrote the same bytes twice over, and still agreed on
    # no number: a comparison that reads "not unequal" as agreement would
    # pass an onboarding whose program computes nothing.
    result = check(harness, reference_builder(value=float("nan")),
                   reference(harness.tmp_path))

    assert result.verdict == "fail"
    comparison = result.detail["runs"][0]["outputs"][0]
    assert comparison["deterministic"] is True
    assert comparison["comparison_result"]["pass"] is False
