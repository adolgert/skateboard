from pathlib import Path

import pytest

from equivalent.components import timing
from equivalent.components.errors import ComponentError
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.tests.fakes import FakeBuilder

TREE = Subject(kind="tree", sha256="1" * 64)


def _store_with_build_claim(tmp_path, flags=("-O2", "-stdpar=gpu")):
    store = LedgerStore(tmp_path / "region")
    store.record_claim(
        [TREE], "build/replay",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass",
                  detail={"flags": list(flags)}),
        [], "sess-1",
    )
    return store


def test_port_pass_reports_the_measured_runs_and_the_build_claims_flags(tmp_path):
    builder = FakeBuilder()
    store = _store_with_build_claim(tmp_path)

    result = timing.check_port(store, TREE, "ch04:step", "tree123", builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["runs_s"] == builder.runs_s
    # The timing claim records the flags the binary was actually built
    # with, read back from the tree's own build/replay claim.
    assert result["detail"]["flags"] == ["-O2", "-stdpar=gpu"]


def test_port_fail_when_the_binary_is_not_built(tmp_path):
    builder = FakeBuilder()
    builder.time_ok = False
    store = _store_with_build_claim(tmp_path)

    result = timing.check_port(store, TREE, "ch04:step", "tree123", builder)

    assert result["verdict"] == "fail"


def test_port_refuses_to_time_a_tree_with_no_passing_build_claim(tmp_path):
    builder = FakeBuilder()
    store = LedgerStore(tmp_path / "region")

    with pytest.raises(ComponentError):
        timing.check_port(store, TREE, "ch04:step", "tree123", builder)


def test_baseline_builds_the_pristine_tree_under_the_fixed_cpu_profile_then_times_it(tmp_path):
    seed = tmp_path / "seed" / "src"
    seed.mkdir(parents=True)
    (seed / "mod_kernel.f90").write_text("module mod_kernel\nend module\n")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, tmp_path / "seed")
    builder = FakeBuilder()

    result = timing.check_baseline(repo_dir, "ch04:step", "basetree123", builder)

    assert result["verdict"] == "pass"
    assert builder.build_calls[0]["profile"] == "cpu_best"
    assert builder.time_calls[0]["attempt_id"] == builder.build_calls[0]["attempt_id"]


def test_baseline_fail_when_the_baseline_itself_does_not_build(tmp_path):
    seed = tmp_path / "seed" / "src"
    seed.mkdir(parents=True)
    (seed / "mod_kernel.f90").write_text("module mod_kernel\nend module\n")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, tmp_path / "seed")
    builder = FakeBuilder()
    builder.build_ok = False

    result = timing.check_baseline(repo_dir, "ch04:step", "basetree123", builder)

    assert result["verdict"] == "fail"
    assert builder.time_calls == []
