import json

import pytest

from equivalent.ledger.records import Claim, Predicate, RequestLogLine
from equivalent.ledger.subjects import Subject


def _claim():
    return Claim(
        id="c-0001",
        ts="2026-09-03T14:12:07Z",
        subject=(Subject(kind="tree", sha256="a" * 64),),
        predicateType="build/replay",
        predicate=Predicate(tool="builder", version="0.1.0", configHash="b" * 64, verdict="pass", detail={}),
        materials=(Subject(kind="strategy", sha256="c" * 64),),
        session="pi-2026-09-03-8f2a",
    )


def test_claim_round_trips_through_json_without_loss():
    claim = _claim()
    restored = Claim.from_dict(json.loads(json.dumps(claim.to_dict())))
    assert restored == claim


def test_claim_rejects_unknown_field_on_load():
    d = _claim().to_dict()
    d["extra_field"] = "surprise"
    with pytest.raises(ValueError):
        Claim.from_dict(d)


def test_claim_defaults_schema_version():
    claim = _claim()
    assert claim.version == 1


def _request_line(**overrides):
    fields = dict(
        ts="2026-09-03T14:12:07Z", session="pi-2026-09-03-8f2a", model="claude-sonnet-5",
        endpoint="run", action="sese_check", region="ch04:step", tree="a" * 64,
        config_hash="b" * 64, outcome="claim", claim_id="c-0001",
    )
    return RequestLogLine(**{**fields, **overrides})


def test_request_line_round_trips_with_the_tool_call_that_made_it():
    line = _request_line(tool_call_id="tool:1787913327226:mu24lznsjv")

    restored = RequestLogLine.from_dict(json.loads(json.dumps(line.to_dict())))

    assert restored == line
    assert restored.tool_call_id == "tool:1787913327226:mu24lznsjv"


def test_a_request_line_from_a_caller_that_is_not_a_tool_call_omits_the_field():
    line = _request_line()

    assert "tool_call_id" not in line.to_dict()


def test_a_line_written_before_the_field_existed_still_loads():
    old = _request_line().to_dict()

    assert RequestLogLine.from_dict(old).tool_call_id is None
