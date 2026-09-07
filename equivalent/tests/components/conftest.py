"""How a component test builds the context a check is handed.

A check is a function of what it is given, so a test of one is a test of
what happens when it is given something in particular: a tree, the
strategies it is judged under, a claim it rests on, and the sets that
claim named. This builds all of that the way the gateway builds it, so a
test cannot accidentally hand a check something the gateway never would.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from equivalent.components import harness_capture
from equivalent.components.context import CheckContext
from equivalent.ledger.capture_sets import SetReader, write_dataset
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy, load_strategy
from equivalent.tests.fakes import FakeBuilder, FakeOracle, fixture_arrays, write_tree
from equivalent.tree import Tree, init_baseline_repo

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
# The three strategy files these tests build against: what a port is
# built with, what the baseline is built with, and the wide-open one a
# code is brought in under.
PORT_STRATEGY = "stdpar_managed"
BASELINE_STRATEGY = "cpu_reference"
ONBOARDING_STRATEGY = "onboarding"


def strategy(name: str) -> Strategy:
    """One of the strategy files the deployment ships, by name."""
    return load_strategy(STRATEGY_DIR / f"{name}.yaml")


def write_visible_dataset(directory, offsets=(0,)) -> Path:
    """A visible dataset on disk, one input case per offset.

    A region's visible inputs are files the check reads for itself, so a
    test that wants particular cases writes them where a deployment keeps
    them rather than handing the check a dictionary.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_dataset(
        directory,
        {f"case{i:04d}": {"inputs": fixture_arrays(offset)} for i, offset in enumerate(offsets)},
        outputs=False,
    )
    return directory


class Harness:
    """A region on disk and the pieces a check is handed about it."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.store = LedgerStore(tmp_path / "ledger")
        self.builder = FakeBuilder()
        self.oracle = FakeOracle()
        self.claims: dict = {}
        self.repo_dir = tmp_path / "repo"

    def repo(self, seed=None) -> Path:
        """A repository whose baseline is `seed`, or the fixture code's tree.

        Asking twice is asking for the same repository: a test that runs
        a check twice is running it against one tree, not two.
        """
        if not (self.repo_dir / ".git").is_dir():
            init_baseline_repo(
                self.repo_dir, write_tree(self.tmp_path / "seed") if seed is None else seed,
            )
        return self.repo_dir

    @property
    def tree(self) -> Tree:
        return Tree(self.repo_dir, "main")

    @property
    def subject(self) -> Subject:
        """The tree these claims are about, or a stand-in before there is one.

        A check reads a claim out of the context by predicate type, so a
        test may file one before it has made the repository the claim
        would be about.
        """
        sha = self.tree.sha if (self.repo_dir / ".git").is_dir() else "0" * 64
        return Subject(kind="tree", sha256=sha)

    def claim(self, predicate_type: str, detail: dict, *, verdict: str = "pass") -> object:
        """File one claim and put it where the gateway would put it.

        The check reads it out of the context, so what makes this claim
        reachable is the same thing that makes it reachable in the
        gateway: the precondition table asked for it.
        """
        claim = self.store.record_claim(
            [self.subject], predicate_type,
            Predicate(tool="builder", version="0.1", configHash="cfg",
                      verdict=verdict, detail=detail),
            [], "sess-1",
        )
        self.claims[predicate_type] = claim
        return claim

    def captured(self):
        """A tree whose capture check has passed, with its sets in the ledger.

        The onboarding checks that follow the capture compare against the
        sets it approved, so each of their tests starts from a real run of
        it rather than from a hand-written claim: a set the capture check
        would not have produced is not one they will ever be given.
        """
        self.repo()
        result = harness_capture.check(self.context(builder=FakeBuilder()), {})
        assert result.verdict == "pass"
        self.keep(result)
        self.claim(harness_capture.CAPTURED_PREDICATE, result.detail)
        return result

    def keep(self, result) -> None:
        """File what a check packed, the way the gateway files it."""
        for packed in result.stores:
            self.store.keep(packed)

    def context(self, **overrides) -> CheckContext:
        fields = {
            "region_id": "tsunami:onboarding",
            "phase": "onboarding",
            "tree": self.tree,
            "baseline": Tree.baseline(self.repo_dir),
            "strategy": strategy(PORT_STRATEGY),
            "baseline_strategy": strategy(BASELINE_STRATEGY),
            "builder": self.builder,
            "oracle": self.oracle,
            "claims": self.claims,
            "sets": SetReader(self.store.capture_sets_dir),
        }
        fields.update(overrides)
        return CheckContext(**fields)


@pytest.fixture
def harness(tmp_path) -> Harness:
    return Harness(tmp_path)
