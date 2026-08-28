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
from pathlib import Path

import pytest

from equivalent.components import harness_capture, harness_self_check
from equivalent.components.errors import ComponentError
from equivalent.gateway.submit import attempt_id_for_strategy, init_baseline_repo
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import (
    TOLERANCES_IN_TREE,
    FakeBuilder,
    mutant_row,
    write_tree,
)

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
REGION = "tsunami:onboarding"
TREE_SHA = "a" * 64
TREE = Subject(kind="tree", sha256=TREE_SHA)


def _baseline_strategy():
    return load_strategy(STRATEGY_DIR / "cpu_reference.yaml")


def _captured(tmp_path):
    """A region whose tree has captured, so there is a visible set to score against."""
    repo = tmp_path / "repo"
    seed = write_tree(tmp_path / "seed")
    init_baseline_repo(repo, seed)
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
    return repo, store, seed


def _check(tmp_path, builder, **kwargs):
    repo, store, _ = _captured(tmp_path)
    return harness_self_check.check(
        store, TREE, repo, "main", REGION, TREE_SHA, _baseline_strategy(), builder, **kwargs,
    )


def test_a_harness_that_kills_a_mutant_and_hides_none_passes(tmp_path):
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["counts"]["KILLED"] == 1
    assert result["detail"]["gap"] == []


def test_a_pass_lists_the_survivors_for_the_person_to_read(tmp_path):
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    survivors = result["detail"]["survivors"]
    assert [row["id"] for row in survivors] == ["m-0002"]
    # Enough to open the file at that line and decide which kind of
    # survivor it is; nothing here can tell them apart.
    assert survivors[0]["file"] == "src/mod_kernel.f90"
    assert survivors[0]["line"] == 42
    assert survivors[0]["mutated"].strip()


def test_a_mutant_the_bands_let_through_fails_and_is_named(tmp_path):
    builder = FakeBuilder()
    builder.mutate_results = [
        mutant_row("m-0001", "KILLED"),
        mutant_row("m-0007", "GAP", line=19, op="CRP", note="case 'case0000': changed within the band: h"),
    ]

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    gap = result["detail"]["gap"]
    assert [row["id"] for row in gap] == ["m-0007"]
    assert gap[0]["line"] == 19
    assert gap[0]["op"] == "CRP"
    assert gap[0]["mutated"].strip()
    assert any("tolerance" in problem for problem in result["detail"]["problems"])


def test_a_harness_that_kills_nothing_fails(tmp_path):
    # Every mutant survives: the region's answers can be changed and no
    # comparison this harness makes would say so.
    builder = FakeBuilder()
    builder.mutate_results = [
        mutant_row("m-0001", "EQUIVALENT"),
        mutant_row("m-0002", "EQUIVALENT"),
    ]

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert any("killed" in problem for problem in result["detail"]["problems"])


def test_a_region_no_mutant_could_be_made_of_fails(tmp_path):
    builder = FakeBuilder()
    builder.mutate_results = []
    builder.generated = 0

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["generated"] == 0
    assert any("no mutant" in problem for problem in result["detail"]["problems"])


def test_the_builder_is_asked_for_the_regions_files_under_the_baseline_strategy(tmp_path):
    builder = FakeBuilder()

    _check(tmp_path, builder)

    call = builder.mutate_calls[0]
    assert call["files"] == ["src/mod_kernel.f90"]
    assert call["makefile"] == "Makefile"
    assert call["replay_target"] == {"target": "replay", "executable": "replay"}
    # The build a port's answers are compared against is the baseline's,
    # so the mutants are built the way the baseline is built.
    assert call["compiler"] == "nvfortran"
    assert call["flags"] == ["-O2", "-stdpar=multicore"]
    assert call["attempt_id"] == attempt_id_for_strategy(REGION, TREE_SHA, "cpu_reference")
    # The visible capture set, in and out: the inputs to replay and the
    # answers to score against.
    assert sorted(call["cases"]) == ["case0000", "case0001"]
    assert sorted(call["cases"]["case0000"]) == ["inputs", "outputs"]
    # And the bands a port is judged within, from the tree's own policy.
    assert sorted(call["bands"]) == ["field", "flux"]


def test_a_limit_the_caller_names_reaches_the_builder(tmp_path):
    builder = FakeBuilder()
    builder.generated = 90

    result = _check(tmp_path, builder, limit=2)

    assert builder.mutate_calls[0]["limit"] == 2
    # And the claim says how many there were, not only how many were run.
    assert result["detail"]["generated"] == 90
    assert result["detail"]["scored"] == 2


def test_the_verdict_names_the_capture_set_and_the_policy_it_rests_on(tmp_path):
    repo, store, seed = _captured(tmp_path)
    builder = FakeBuilder()

    result = harness_self_check.check(
        store, TREE, repo, "main", REGION, TREE_SHA, _baseline_strategy(), builder,
    )

    policy = (seed / TOLERANCES_IN_TREE).read_bytes()
    assert result["detail"]["policy_sha256"] == hashlib.sha256(policy).hexdigest()
    visible = result["detail"]["datasets"]["visible"]
    assert visible["cases"] == 2
    assert len(visible["capture_set"]) == 64


def test_a_builder_that_could_not_run_the_mutation_is_an_error_not_a_verdict(tmp_path):
    class Refusing(FakeBuilder):
        def mutate(self, *args, **kwargs):
            return {"ok": False, "stage": "mutate", "generated": 0, "scored": 0,
                    "results": [], "counts": {}, "kept_dirs": [],
                    "log_tail": "there is no built tree for attempt 'x'"}

    with pytest.raises(ComponentError) as excinfo:
        _check(tmp_path, Refusing())

    assert "no built tree" in str(excinfo.value)
