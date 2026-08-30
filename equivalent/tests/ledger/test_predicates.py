from equivalent.ledger import predicates
from equivalent.ledger.predicates import DetailLevel
from equivalent.ledger.records import Predicate


def test_every_predicate_declares_deterministic_and_agent_detail():
    assert predicates.PREDICATE_TYPES, "registry must not be empty"
    for name, pt in predicates.PREDICATE_TYPES.items():
        assert pt.name == name
        assert isinstance(pt.deterministic, bool)
        assert isinstance(pt.agent_detail, DetailLevel)
        assert pt.description, f"{name} has no description"


def test_expected_predicate_types_present():
    expected = {
        "sese/verified", "build/replay", "gpu/executed",
        "sanitize/memcheck", "sanitize/racecheck", "sanitize/initcheck",
        "regression/visible", "regression/holdout",
        "timing/port", "timing/baseline",
    }
    assert expected <= set(predicates.PREDICATE_TYPES)


def test_no_predicate_name_carries_a_namespace_prefix():
    # Settled 2026-08-27: bare predicate types, no v1./project. prefix.
    for name in predicates.PREDICATE_TYPES:
        assert "/" in name
        prefix = name.split("/", 1)[0]
        assert "." not in prefix


def test_timing_predicates_are_nondeterministic_everything_else_is_not():
    for name, pt in predicates.PREDICATE_TYPES.items():
        if name.startswith("timing/"):
            assert pt.deterministic is False
        else:
            assert pt.deterministic is True


def test_holdout_is_the_only_verdict_only_predicate():
    verdict_only = {n for n, pt in predicates.PREDICATE_TYPES.items() if pt.agent_detail is DetailLevel.VERDICT_ONLY}
    assert verdict_only == {"regression/holdout"}


def test_agent_receipt_drops_detail_for_verdict_only_predicates():
    pred = Predicate(tool="oracle", version="0.3.1", configHash="cfg", verdict="pass",
                      detail={"case0003": {"field": {"max_rel": 2.1e-3, "pass": False}}})
    receipt = predicates.agent_receipt("regression/holdout", pred)
    assert receipt == {"verdict": "pass"}


def test_agent_receipt_includes_whatever_detail_the_tool_recorded():
    pred = Predicate(tool="oracle", version="0.3.1", configHash="cfg", verdict="fail",
                      detail={"case0003": {"field": {"max_rel": 2.1e-3, "pass": False}}})
    receipt = predicates.agent_receipt("regression/visible", pred)
    assert receipt == {"verdict": "fail", "detail": pred.detail}
