"""Building an onboarding tree under both of the region's strategies.

These read as the statement of what "the harness builds" means: the same
tree, built twice, and each build has to have succeeded, used the
strategy's own flags, and compiled nothing from outside the tree.
"""
from __future__ import annotations

from functools import partial
from itertools import count

from equivalent.components import harness_build
from equivalent.tree import attempt_id_for_strategy
from equivalent.tests.components.conftest import (
    BASELINE_STRATEGY,
    ONBOARDING_STRATEGY,
    strategy as strategy_named,
)
from equivalent.tests.fakes import FakeBuilder, built

REGION = "tsunami:onboarding"


def drops_the_flags_after_one_build():
    """A builder whose second build ignores the flags it was given.

    A makefile that honors one compiler's flags and hard-codes another's
    is exactly the thing two builds are asked for; this is that makefile.
    """
    builds = count()
    return FakeBuilder(
        build=lambda request: built(request, flags_reached_every_compile=next(builds) == 0),
    )


def _strategies():
    return strategy_named(ONBOARDING_STRATEGY), strategy_named(BASELINE_STRATEGY)


def _check(harness, builder):
    strategy, baseline = _strategies()
    harness.repo()
    return harness_build.check(
        harness.context(region_id=REGION, strategy=strategy, baseline_strategy=baseline,
                        builder=builder),
        {},
    )


def test_both_builds_succeeding_is_a_pass_that_records_each_one(harness):
    builder = FakeBuilder()

    result = _check(harness, builder)

    assert result.verdict == "pass"
    assert result.detail["failed_strategies"] == []
    assert sorted(result.detail["strategies"]) == ["cpu_reference", "onboarding"]
    # Every target the tree's own manifest declares was asked for.
    assert result.detail["targets_asked_for"] == ["replay", "timing", "capture"]
    assert len(builder.build_calls) == 2


def test_each_strategy_builds_in_a_workspace_of_its_own(harness):
    # Two builds of one tree sharing a workspace would leave the second
    # reading the first's object files.
    builder = FakeBuilder()

    _check(harness, builder)

    attempts = [call["attempt_id"] for call in builder.build_calls]
    assert attempts == [
        attempt_id_for_strategy(REGION, harness.tree.sha, "cpu_reference"),
        attempt_id_for_strategy(REGION, harness.tree.sha, "onboarding"),
    ]
    assert len(set(attempts)) == 2


def test_a_build_that_ignored_the_second_strategys_flags_fails_and_names_that_strategy(harness):
    builder = drops_the_flags_after_one_build()

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["failed_strategies"] == ["onboarding"]
    # And the failing half says which of the three statements did not hold,
    # with the command line that broke it.
    onboarding = result.detail["strategies"]["onboarding"]
    assert onboarding["compiles_without_flags"]
    assert "cpu_reference" not in result.detail["failed_strategies"]


def test_a_build_that_did_not_compile_at_all_fails(harness):
    builder = FakeBuilder(build=partial(built, ok=False))

    result = _check(harness, builder)

    assert result.verdict == "fail"
    assert result.detail["failed_strategies"] == ["cpu_reference", "onboarding"]


def test_the_flags_each_build_used_are_the_strategys_own(harness):
    builder = FakeBuilder()
    strategy, baseline = _strategies()

    _check(harness, builder)

    used = [call["flags"] for call in builder.build_calls]
    assert used == [
        list(baseline.languages["fortran"].flags),
        list(strategy.languages["fortran"].flags),
    ]
