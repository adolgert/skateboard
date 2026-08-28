"""Building an onboarding tree under both of the region's strategies.

These read as the statement of what "the harness builds" means: the same
tree, built twice, and each build has to have succeeded, used the
strategy's own flags, and compiled nothing from outside the tree.
"""
from __future__ import annotations

from pathlib import Path

from equivalent.components import harness_build
from equivalent.gateway.submit import attempt_id_for_strategy, init_baseline_repo
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, write_tree

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
REGION = "tsunami:onboarding"


class BuilderThatDropsTheFlagsAfterOneBuild(FakeBuilder):
    """A builder whose second build ignores the flags it was given.

    A makefile that honors one compiler's flags and hard-codes another's
    is exactly the thing two builds are asked for; this is that makefile.
    """

    def build(self, *args, **kwargs):
        result = super().build(*args, **kwargs)
        self.flags_reached = False
        return result


def _repo(tmp_path):
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed"))
    return repo


def _strategies():
    return (
        load_strategy(STRATEGY_DIR / "onboarding.yaml"),
        load_strategy(STRATEGY_DIR / "cpu_reference.yaml"),
    )


def _check(repo, builder, tree_sha="a" * 64):
    strategy, baseline = _strategies()
    return harness_build.check(repo, "main", REGION, tree_sha, strategy, baseline, builder)


def test_both_builds_succeeding_is_a_pass_that_records_each_one(tmp_path):
    builder = FakeBuilder()

    result = _check(_repo(tmp_path), builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["failed_strategies"] == []
    assert sorted(result["detail"]["strategies"]) == ["cpu_reference", "onboarding"]
    # Every target the tree's own manifest declares was asked for.
    assert result["detail"]["targets_asked_for"] == ["replay", "timing"]
    assert len(builder.build_calls) == 2


def test_each_strategy_builds_in_a_workspace_of_its_own(tmp_path):
    # Two builds of one tree sharing a workspace would leave the second
    # reading the first's object files.
    builder = FakeBuilder()

    _check(_repo(tmp_path), builder, tree_sha="b" * 64)

    attempts = [call["attempt_id"] for call in builder.build_calls]
    assert attempts == [
        attempt_id_for_strategy(REGION, "b" * 64, "cpu_reference"),
        attempt_id_for_strategy(REGION, "b" * 64, "onboarding"),
    ]
    assert len(set(attempts)) == 2


def test_a_build_that_ignored_the_second_strategys_flags_fails_and_names_that_strategy(tmp_path):
    builder = BuilderThatDropsTheFlagsAfterOneBuild()

    result = _check(_repo(tmp_path), builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["failed_strategies"] == ["onboarding"]
    # And the failing half says which of the three statements did not hold,
    # with the command line that broke it.
    onboarding = result["detail"]["strategies"]["onboarding"]
    assert onboarding["compiles_without_flags"]
    assert "cpu_reference" not in result["detail"]["failed_strategies"]


def test_a_build_that_did_not_compile_at_all_fails(tmp_path):
    builder = FakeBuilder()
    builder.build_ok = False

    result = _check(_repo(tmp_path), builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["failed_strategies"] == ["cpu_reference", "onboarding"]


def test_the_flags_each_build_used_are_the_strategys_own(tmp_path):
    builder = FakeBuilder()
    strategy, baseline = _strategies()

    _check(_repo(tmp_path), builder)

    used = [call["flags"] for call in builder.build_calls]
    assert used == [
        list(baseline.languages["fortran"].flags),
        list(strategy.languages["fortran"].flags),
    ]
