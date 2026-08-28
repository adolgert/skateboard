from pathlib import Path

import pytest

from equivalent.components import regression
from equivalent.components.errors import ComponentError
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, FakeOracle, fixture_case, write_program

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
TREE = Subject(kind="tree", sha256="a" * 64)


def test_visible_reads_outputs_from_the_stored_gpu_executed_claim_not_a_new_run(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    outputs = {"case0000": fixture_case()}
    store.record_claim([TREE], "gpu/executed",
                        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass",
                                  detail={"kernels_launched": 4, "outputs": outputs}),
                        [], "sess-0")
    oracle = FakeOracle()

    result = regression.check_visible(store, TREE, oracle)

    assert result["verdict"] == "pass"
    assert oracle.compare_calls[0]["outputs"] == outputs


def test_visible_raises_component_error_with_no_passing_run(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    oracle = FakeOracle()

    with pytest.raises(ComponentError):
        regression.check_visible(store, TREE, oracle)


def _manifest(tmp_path):
    """The code's own description, which is where the replay executable is named."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def test_holdout_never_puts_outputs_or_per_case_detail_in_its_own_claim(tmp_path):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    oracle = FakeOracle()

    result = regression.check_holdout("ch04:step", "tree123", strategy, _manifest(tmp_path), oracle, builder)

    assert result["verdict"] == "pass"
    assert "outputs" not in result["detail"]
    assert "per_case" not in result["detail"]


def test_holdout_fetches_inputs_from_the_oracle_and_runs_them_through_the_builder(tmp_path):
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    oracle = FakeOracle()

    regression.check_holdout("ch04:step", "tree123", strategy, _manifest(tmp_path), oracle, builder)

    assert list(builder.run_calls[0]["cases"]) == ["hcase0"]
    assert oracle.compare_calls[0]["dataset"] == "holdout"
