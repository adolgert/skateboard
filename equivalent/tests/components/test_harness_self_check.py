"""Asking the harness about itself: would it notice a wrong port of this region.

These read as the statement of what the self-check means. A harness that
kills nothing is not a harness. A mutant that changes an answer and slips
through the tolerance bands is worse than one that is caught, because the
bands are what a port is judged by -- so it fails the check and is named.
And a survivor is not a failure at all: it is a line the captured inputs
never reach, or code that really is equivalent, and it is listed for the
person to read.
"""
from __future__ import annotations

import hashlib
from functools import partial

import pytest

from equivalent.components import harness_self_check
from equivalent.components.errors import ComponentError
from equivalent.gateway.backend_client import MutateResponse
from equivalent.tree import attempt_id_for_strategy
from equivalent.tests.fakes import TOLERANCES_IN_TREE, FakeBuilder, mutant_row, mutated

REGION = "tsunami:onboarding"


def _check(harness, builder, **config):
    """The self-check on a tree whose capture has already passed."""
    harness.captured()
    return harness_self_check.check(harness.context(region_id=REGION, builder=builder), config)


def test_a_harness_that_kills_a_mutant_and_hides_none_passes(harness):
    builder = harness.builder

    result = _check(harness, builder)

    assert result.verdict == "pass"
    assert result.detail["counts"]["KILLED"] == 1
    assert result.detail["gap"] == []


def test_a_pass_lists_the_survivors_for_the_person_to_read(harness):
    builder = harness.builder

    result = _check(harness, builder)

    survivors = result.detail["survivors"]
    assert [row["id"] for row in survivors] == ["m-0002"]
    # Enough to open the file at that line and decide which kind of
    # survivor it is; nothing here can tell them apart.
    assert survivors[0]["file"] == "src/mod_kernel.f90"
    assert survivors[0]["line"] == 42
    assert survivors[0]["mutated"].strip()


def test_a_mutant_the_bands_let_through_fails_and_is_named(harness):
    builder = FakeBuilder(mutate=partial(mutated, results=[
        mutant_row("m-0001", "KILLED"),
        mutant_row("m-0007", "GAP", line=19, op="CRP", note="case 'case0000': changed within the band: h"),
    ]))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    gap = result.detail["gap"]
    assert [row["id"] for row in gap] == ["m-0007"]
    assert gap[0]["line"] == 19
    assert gap[0]["op"] == "CRP"
    assert gap[0]["mutated"].strip()
    assert any("tolerance" in problem for problem in result.detail["problems"])


def test_a_harness_that_kills_nothing_fails(harness):
    # Every mutant survives: the region's answers can be changed and no
    # comparison this harness makes would say so.
    builder = FakeBuilder(mutate=partial(mutated, results=[
        mutant_row("m-0001", "EQUIVALENT"),
        mutant_row("m-0002", "EQUIVALENT"),
    ]))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert any("killed" in problem for problem in result.detail["problems"])


def test_a_region_no_mutant_could_be_made_of_fails(harness):
    builder = FakeBuilder(mutate=partial(mutated, results=[]))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["generated"] == 0
    assert any("no mutant" in problem for problem in result.detail["problems"])


def test_the_builder_is_asked_for_the_regions_files_under_the_baseline_strategy(harness):
    builder = harness.builder

    _check(harness, builder)

    call = builder.mutate_calls[0]
    assert call["files"] == ["src/mod_kernel.f90"]
    assert call["makefile"] == "Makefile"
    assert call["replay_target"] == {"target": "replay", "executable": "replay"}
    # The build a port's answers are compared against is the baseline's,
    # so the mutants are built the way the baseline is built.
    assert call["compiler"] == "nvfortran"
    assert call["flags"] == ["-O2", "-stdpar=multicore"]
    assert call["attempt_id"] == attempt_id_for_strategy(REGION, harness.tree.sha, "cpu_reference")
    # The visible capture set, in and out: the inputs to replay and the
    # answers to score against.
    assert sorted(call["cases"]) == ["case0000", "case0001"]
    assert sorted(call["cases"]["case0000"]) == ["inputs", "outputs"]
    # And the bands a port is judged within, from the tree's own policy.
    assert sorted(call["bands"]) == ["field", "flux"]


def test_a_limit_the_caller_names_reaches_the_builder(harness):
    builder = FakeBuilder(mutate=partial(mutated, generated=90))

    result = _check(harness, builder, limit=2)

    assert builder.mutate_calls[0]["limit"] == 2
    # And the claim says how many there were, not only how many were run.
    assert result.detail["generated"] == 90
    assert result.detail["scored"] == 2
    assert result.verdict == "fail"
    assert any("not all generated mutants" in p for p in result.detail["problems"])


@pytest.mark.parametrize("status", ["SKIPPED", "PENDING", "RUNTIME_FAIL"])
def test_an_incomplete_mutant_prevents_an_adequacy_pass(harness, status):
    builder = FakeBuilder(mutate=partial(mutated, results=[
        mutant_row("m-0001", "KILLED"),
        mutant_row("m-0002", status),
    ]))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["incomplete"][0]["id"] == "m-0002"


def test_malformed_or_inconsistent_mutation_counts_fail_closed(harness):
    inconsistent = FakeBuilder(mutate=partial(mutated, scored=99))

    result = _check(harness, inconsistent)

    assert result.verdict == "fail"
    assert any("inconsistent" in p for p in result.detail["problems"])


def test_the_verdict_names_the_capture_set_and_the_policy_it_rests_on(harness):
    result = _check(harness, harness.builder)

    policy = (harness.tmp_path / "seed" / TOLERANCES_IN_TREE).read_bytes()
    assert result.detail["policy_sha256"] == hashlib.sha256(policy).hexdigest()
    visible = result.detail["datasets"]["visible"]
    assert visible["cases"] == 2
    assert len(visible["capture_set"]) == 64


def test_a_builder_that_could_not_run_the_mutation_is_an_error_not_a_verdict(harness):
    refusing = FakeBuilder(mutate=MutateResponse(
        ok=False, log_tail="there is no built tree for attempt 'x'",
    ))

    with pytest.raises(ComponentError) as excinfo:
        _check(harness, refusing)

    assert "no built tree" in str(excinfo.value)
