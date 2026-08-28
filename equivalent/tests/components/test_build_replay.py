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
