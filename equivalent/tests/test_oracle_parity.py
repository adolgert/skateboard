"""The two spellings of the oracle's wire, and what keeps them the same.

The oracle image installs numpy, yaml and the web server and nothing else
of this project, and the gateway image installs nothing of the services,
so neither side can import the other's contract: services/oracle/
contract.py says what the oracle answers, and
equivalent/components/answers.py says what the checks read. Only a test
can hold the two up against each other, and this is it -- the same job
test_builder_parity.py does for the builder.

Two things have to hold. A response field a check reads and the oracle
never writes is a default read as an answer: no failing case, no policy.
And a field the oracle leaves out on purpose -- the held-out set's
per-case detail -- has to read on this side as "there is none", not as a
verdict about the cases.
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from equivalent.components import answers
from equivalent.components.errors import ComponentError
from services.oracle import contract

# Which oracle response each gateway-side answer is the reading half of.
# The two sides of a pair are deliberately spelled with the same name.
PAIRS = [
    (answers.PolicyResponse, contract.PolicyResponse),
    (answers.HoldoutInputsResponse, contract.HoldoutInputsResponse),
    (answers.CompareResponse, contract.CompareResponse),
]
PAIR_IDS = [read.__name__ for read, _ in PAIRS]


@pytest.mark.parametrize("read,written", PAIRS, ids=PAIR_IDS)
def test_every_field_a_check_reads_is_one_the_oracle_writes(read, written):
    for field in dataclass_fields(read):
        assert field.name in written.model_fields, (
            f"{read.__name__} reads '{field.name}', which the oracle's "
            f"{written.__name__} does not write"
        )


def test_the_held_out_answer_carries_no_per_case_detail_on_either_side():
    # The oracle leaves the key out for the held-out dataset rather than
    # sending an empty one, and this side reads its absence as nothing to
    # report -- never as a set of cases that all agreed.
    assert contract.CompareResponse.model_fields["per_case"].get_default() is None

    holdout = answers.CompareResponse.parse({"verdict": "pass", "policy_sha256": "f" * 64})

    assert holdout.per_case == {}
    assert holdout.cases_that_failed() == []


@pytest.mark.parametrize("body", [
    {},
    {"policy_sha256": "f" * 64},
    {"verdict": "pass"},
    {"verdict": "maybe", "policy_sha256": "f" * 64},
    {"verdict": "pass", "policy_sha256": "not a digest"},
])
def test_a_comparison_the_oracle_did_not_finish_saying_is_no_verdict(body):
    # Every one of these fields is required on the oracle's side, so an
    # answer missing one is an oracle that did not answer -- a fault on
    # the harness's side rather than a port that is wrong.
    with pytest.raises(ComponentError):
        answers.CompareResponse.parse(body)


def test_an_oracle_that_names_no_identity_cannot_be_pinned():
    with pytest.raises(ComponentError):
        answers.PolicyResponse.parse({"policy_sha256": "f" * 64})
