from pathlib import Path

import pytest

from equivalent.components import build_replay
from equivalent.components.errors import ComponentError
from equivalent.gateway.submit import init_baseline_repo
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"


def _repo(tmp_path, files):
    seed = tmp_path / "seed" / "src"
    seed.mkdir(parents=True)
    for name, content in files.items():
        (seed / name).write_text(content)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, tmp_path / "seed")
    return repo_dir


def test_pass_sends_every_fortran_file_in_the_tree(tmp_path):
    repo_dir = _repo(tmp_path, {
        "mod_params.f90": "module mod_params\nend module\n",
        "mod_diff.f90": "module mod_diff\nend module\n",
        "mod_kernel.f90": "module mod_kernel\nend module\n",
    })
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    result = build_replay.check(repo_dir, "main", "ch04:step", "tree123", strategy, builder)

    assert result["verdict"] == "pass"
    sent_paths = {f["path"] for f in builder.build_calls[0]["files"]}
    assert sent_paths == {"src/mod_params.f90", "src/mod_diff.f90", "src/mod_kernel.f90"}
    assert builder.build_calls[0]["profile"] == "stdpar_managed"


def test_the_strategy_files_flags_reach_the_builder_and_the_claim_detail(tmp_path):
    # The hashed strategy YAML fixes the flags. The builder must receive
    # them explicitly (not look up its own profile table), and the claim
    # records what the builder says it actually compiled with.
    repo_dir = _repo(tmp_path, {"mod_kernel.f90": "module mod_kernel\nend module\n"})
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    result = build_replay.check(repo_dir, "main", "ch04:step", "tree123", strategy, builder)

    expected = list(strategy.languages["fortran"].flags)
    assert builder.build_calls[0]["flags"] == expected
    assert builder.build_calls[0]["link_flags"] == list(strategy.link_flags)
    assert result["detail"]["flags"] == expected  # link_flags is empty today


def test_fail_when_the_builder_reports_a_compile_error(tmp_path):
    repo_dir = _repo(tmp_path, {"mod_kernel.f90": "module mod_kernel\nend module\n"})
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()
    builder.build_ok = False

    result = build_replay.check(repo_dir, "main", "ch04:step", "tree123", strategy, builder)

    assert result["verdict"] == "fail"
    assert "log_tail" in result["detail"]


def test_raises_component_error_with_no_fortran_files(tmp_path):
    repo_dir = _repo(tmp_path, {"README.md": "hello\n"})
    strategy = load_strategy(STRATEGY_PATH)
    builder = FakeBuilder()

    with pytest.raises(ComponentError):
        build_replay.check(repo_dir, "main", "ch04:step", "tree123", strategy, builder)
