"""Replaying the captured inputs and demanding the captured outputs back.

These read as the statement of what "the harness replays" means: the
replay driver, given the inputs the capture program recorded, writes the
outputs the capture program recorded -- bitwise, not within a band. The
tolerance policy is for judging a port; the driver and the capture
program are two halves of one harness, and they either agree exactly or
the harness is not describing the code.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from equivalent.capture import npy
from equivalent.components import harness_capture, harness_replay
from equivalent.components.errors import ComponentError
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


def _captured(tmp_path):
    """A region whose tree has a passing capture claim and the sets behind it."""
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed"))
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


def _check(tmp_path, builder):
    repo, store = _captured(tmp_path)
    return harness_replay.check(
        store, TREE, repo, "main", REGION, TREE_SHA, _baseline_strategy(), builder,
    )


def _replaying_builder():
    builder = FakeBuilder()
    builder.replays_capture = True
    return builder


def test_a_driver_that_reproduces_every_captured_output_passes(tmp_path):
    builder = _replaying_builder()

    result = _check(tmp_path, builder)

    assert result["verdict"] == "pass"
    datasets = result["detail"]["datasets"]
    assert sorted(datasets) == ["holdout", "visible"]
    assert datasets["visible"]["cases"] == 2
    assert len(datasets["visible"]["capture_set"]) == 64
    # Every captured case was replayed, both datasets in one run each.
    assert [call["executable"] for call in builder.run_calls] == ["replay", "replay"]
    assert builder.run_calls[0]["attempt_id"] == attempt_id_for_strategy(
        REGION, TREE_SHA, "cpu_reference",
    )


def test_the_replay_is_given_the_captured_inputs(tmp_path):
    builder = _replaying_builder()

    _check(tmp_path, builder)

    sent = builder.run_calls[0]["cases"]
    assert sorted(sent) == ["case0000", "case0001"]
    assert sorted(sent["case0000"]) == ["field", "flux"]


class DriftingBuilder(FakeBuilder):
    """A driver whose answer is one element off in one case's one variable."""

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        result = super().run(attempt_id, executable, cases, notify, mandatory)
        drifted = npy.decode(base64.b64decode(result["outputs"]["case0001"]["flux"]))
        drifted[0, 0] += 1
        result["outputs"]["case0001"]["flux"] = base64.b64encode(npy.encode(drifted)).decode()
        return result


def test_one_element_out_of_place_fails_naming_the_case_and_the_variable(tmp_path):
    builder = DriftingBuilder()
    builder.replays_capture = True

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    difference = result["detail"]["datasets"]["visible"]["first_difference"]
    assert difference["case"] == "case0001"
    assert difference["variable"] == "flux"
    assert difference["max_abs"] == 1.0


class SilentBuilder(FakeBuilder):
    """A driver that writes no file at all for one declared output."""

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        result = super().run(attempt_id, executable, cases, notify, mandatory)
        del result["outputs"]["case0000"]["field"]
        return result


def test_an_output_the_driver_never_wrote_fails_naming_it(tmp_path):
    builder = SilentBuilder()
    builder.replays_capture = True

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    difference = result["detail"]["datasets"]["visible"]["first_difference"]
    assert difference["variable"] == "field"
    assert "wrote no" in difference["reason"]


def test_a_replay_that_would_not_run_fails_with_what_the_builder_said(tmp_path):
    builder = _replaying_builder()
    builder.run_ok = False

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert "runtime crash" in result["detail"]["datasets"]["visible"]["log_tail"]


def test_a_tree_with_no_passing_capture_claim_is_an_error_not_a_verdict(tmp_path):
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed"))

    with pytest.raises(ComponentError):
        harness_replay.check(
            LedgerStore(tmp_path / "ledger"), TREE, repo, "main", REGION, TREE_SHA,
            _baseline_strategy(), _replaying_builder(),
        )
