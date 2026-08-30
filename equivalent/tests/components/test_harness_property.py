"""Running a code's own invariants while the code is being brought in.

The point of running them here rather than only on a port is that a
property module which does not pass on the baseline says nothing about
any port: it would fail for every one of them, and the person would learn
that late. The other half is what happens when a code states no
invariants at all -- a passing claim that says so, so that the absence is
a fact in the ledger rather than a row nobody filed.
"""
from __future__ import annotations

from pathlib import Path

from equivalent.components import harness_capture, harness_property
from equivalent.gateway.submit import attempt_id_for_strategy, init_baseline_repo
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, write_tree

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
REGION = "tsunami:onboarding"
TREE_SHA = "a" * 64
TREE = Subject(kind="tree", sha256=TREE_SHA)


def _baseline_strategy():
    return load_strategy(STRATEGY_DIR / "cpu_reference.yaml")


def _captured(tmp_path, *, properties: bool):
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed", properties=properties))
    store = LedgerStore(tmp_path / "ledger")
    result = harness_capture.check(
        store, repo, "main", REGION, TREE_SHA, _baseline_strategy(), FakeBuilder(),
    )
    assert result["verdict"] == "pass"
    store.record_claim(
        [TREE], "harness/captured",
        Predicate(tool="builder", version="0.1", configHash="cfg",
                  verdict=result["verdict"], detail=result["detail"]),
        [], "sess-1",
    )
    return repo, store


def _check(tmp_path, builder, *, properties: bool = True, **kwargs):
    repo, store = _captured(tmp_path, properties=properties)
    return harness_property.check(
        store, TREE, repo, "main", REGION, TREE_SHA, _baseline_strategy(), builder, **kwargs,
    )


def test_properties_that_hold_on_the_baseline_pass(tmp_path):
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["module"] == "harness/properties.py"
    assert result["detail"]["passed"] == 3


def test_a_property_that_does_not_hold_on_the_baseline_fails_with_what_it_printed(tmp_path):
    builder = FakeBuilder()
    builder.properties_ok = False
    builder.properties_counts = {"passed": 1, "failed": 1, "errors": 0}
    builder.properties_log = "Falsifying example: run_replay(h=array([0.]))"

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["failed"] == 1
    assert "Falsifying example" in result["detail"]["log_tail"]


def test_the_run_is_the_baseline_strategys_and_draws_from_the_visible_captures(tmp_path):
    builder = FakeBuilder()

    _check(tmp_path, builder, seed=99, max_examples=7)

    call = builder.properties_calls[0]
    assert call["attempt_id"] == attempt_id_for_strategy(REGION, TREE_SHA, "cpu_reference")
    assert call["executable"] == "replay"
    assert call["seed"] == 99
    assert call["max_examples"] == 7
    # The corpus is the captured visible inputs, and nothing else.
    assert sorted(call["cases"]) == ["case0000", "case0001"]
    assert sorted(call["cases"]["case0000"]) == ["field", "flux"]


def test_a_seed_nobody_named_is_drawn_and_written_into_the_claim(tmp_path):
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    assert result["detail"]["seed"] == builder.properties_calls[0]["seed"]


def test_a_code_that_states_no_invariants_passes_saying_so(tmp_path):
    # Recorded on purpose: the ledger should say that this code declares
    # nothing to search for, rather than leave a row nobody filed.
    builder = FakeBuilder()

    result = _check(tmp_path, builder, properties=False)

    assert result["verdict"] == "pass"
    assert result["detail"]["module"] is None
    assert "properties: null" in result["detail"]["note"]
    assert builder.properties_calls == []
