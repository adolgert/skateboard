"""Running a code's own invariants while the code is being brought in.

The point of running them here rather than only on a port is that a
property module which does not pass on the baseline says nothing about
any port: it would fail for every one of them, and the person would learn
that late. The other half is what happens when a code states no
invariants at all -- a passing claim that says so, so that the absence is
a fact in the ledger rather than a row nobody filed.
"""
from __future__ import annotations


from equivalent.components import harness_property
from equivalent.tree import attempt_id_for_strategy
from equivalent.tests.fakes import write_tree

REGION = "tsunami:onboarding"


def _check(harness, builder, *, properties: bool = True, **config):
    """The property check on a tree whose capture has already passed."""
    harness.repo(write_tree(harness.tmp_path / "seed", properties=properties))
    harness.captured()
    return harness_property.check(harness.context(region_id=REGION, builder=builder), config)


def test_properties_that_hold_on_the_baseline_pass(harness):
    builder = harness.builder

    result = _check(harness, builder)

    assert result.verdict == "pass"
    assert result.detail["module"] == "harness/properties.py"
    assert result.detail["passed"] == 3


def test_a_property_that_does_not_hold_on_the_baseline_fails_with_what_it_printed(harness):
    builder = harness.builder
    builder.properties_ok = False
    builder.properties_counts = {"passed": 1, "failed": 1, "errors": 0}
    builder.properties_log = "Falsifying example: run_replay(h=array([0.]))"

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["failed"] == 1
    assert "Falsifying example" in result.detail["log_tail"]


def test_the_run_is_the_baseline_strategys_and_draws_from_the_visible_captures(harness):
    builder = harness.builder

    _check(harness, builder, seed=99, max_examples=7)

    call = builder.properties_calls[0]
    assert call["attempt_id"] == attempt_id_for_strategy(REGION, harness.tree.sha, "cpu_reference")
    assert call["executable"] == "replay"
    assert call["seed"] == 99
    assert call["max_examples"] == 7
    # The corpus is the captured visible inputs, and nothing else.
    assert sorted(call["cases"]) == ["case0000", "case0001"]
    assert sorted(call["cases"]["case0000"]) == ["field", "flux"]


def test_a_seed_nobody_named_is_drawn_and_written_into_the_claim(harness):
    builder = harness.builder

    result = _check(harness, builder)

    assert result.detail["seed"] == builder.properties_calls[0]["seed"]


def test_a_code_that_states_no_invariants_passes_saying_so(harness):
    # Recorded on purpose: the ledger should say that this code declares
    # nothing to search for, rather than leave a row nobody filed.
    builder = harness.builder

    result = _check(harness, builder, properties=False)

    assert result.verdict == "pass"
    assert result.detail["module"] is None
    assert "properties: null" in result.detail["note"]
    assert builder.properties_calls == []
