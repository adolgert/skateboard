import json

import pytest

from equivalent.ledger.records import Claim, Predicate
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
