"""What every endpoint answers with, whether its stage succeeded or not.

A key an error path forgot to write reads downstream as "this was not
measured" when the truth is "this was not reported". So these hold every
stage to the response type its endpoint declares, and every failed
answer to the whole shape of that type.
"""
from pathlib import Path

import pytest

from services.builder import contract, stages

HARNESS = Path(__file__).resolve().parents[1] / "builder" / "harness"

RESPONSES = [
    contract.BuildResponse, contract.RunResponse, contract.CaptureResponse,
    contract.SanitizeResponse, contract.PropertiesResponse, contract.MutateResponse,
    contract.TimeResponse, contract.ArtifactsResponse, contract.HealthResponse,
]

REPLAY_TARGET = {"role": "replay", "target": "replay", "executable": "replay"}


def refusals(attempt) -> list:
    """One call per stage that fails before it runs anything at all.

    Every one of them refuses for its own reason -- no makefile, no
    executable, no built tree -- which is exactly the path where a
    response shape is easiest to get wrong.
    """
    return [
        (contract.BuildResponse, lambda: stages.build(
            attempt, [], "Makefile", [REPLAY_TARGET], "gfortran", [], [], [],
            harness_dir=HARNESS,
        )),
        (contract.RunResponse, lambda: stages.run(attempt, "replay", {})),
        (contract.CaptureResponse, lambda: stages.capture(attempt, "gen_reference")),
        (contract.SanitizeResponse, lambda: stages.sanitize(
            attempt, "replay", {}, ["memcheck"],
        )),
        (contract.PropertiesResponse, lambda: stages.properties(
            attempt, "replay", "harness/properties.py", {}, seed=1, max_examples=2,
            harness_dir=HARNESS,
        )),
        (contract.MutateResponse, lambda: stages.mutate(
            attempt, "Makefile", REPLAY_TARGET, [], {}, {}, "gfortran", [], [], [],
            harness_dir=HARNESS,
        )),
        (contract.TimeResponse, lambda: stages.time_run(attempt, "whole_program")),
    ]


def test_every_stage_answers_with_the_type_its_endpoint_declares(attempt):
    for declared, call in refusals(attempt):
        answer = call()
        assert isinstance(answer, declared)
        assert answer.ok is False
        assert answer.stage == declared().stage


def test_a_stage_that_refuses_still_fills_in_the_whole_shape(attempt):
    for declared, call in refusals(attempt):
        assert set(call().to_dict()) == set(declared(ok=True).to_dict())


@pytest.mark.parametrize("response", RESPONSES, ids=lambda cls: cls.__name__)
def test_a_failed_answer_carries_every_key_a_successful_one_does(response):
    # Every key, not only the ones a caller reads today: a reader of a
    # claim can tell a false from a missing answer only if both are there.
    assert set(response.failure("it did not happen").to_dict()) == set(response(ok=True).to_dict())


@pytest.mark.parametrize(
    "response", [cls for cls in RESPONSES if cls.message_field], ids=lambda cls: cls.__name__,
)
def test_a_failure_says_why_in_the_field_that_response_reports_in(response):
    answer = response.failure("the tree holds no executable 'replay'").to_dict()

    assert answer["ok"] is False
    assert answer[response.message_field] == "the tree holds no executable 'replay'"


def test_a_response_survives_the_round_trip_a_caller_reads_it_through():
    original = contract.RunResponse(ok=True, outputs={"case0000": {}}, kernels_launched=3)

    assert contract.RunResponse.from_dict(original.to_dict()) == original
