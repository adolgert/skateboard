import base64
from pathlib import Path

import pytest

from equivalent.components import build_replay
from equivalent.components.errors import ComponentError
from equivalent.gateway.submit import init_baseline_repo
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, write_program

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"


def _manifest(tmp_path):
    """The code's own description of how it is built and what its source is."""
    return load_manifest(write_program(tmp_path) / "manifest.yaml")


def _repo(tmp_path, files):
    seed = tmp_path / "seed"
    for name, content in files.items():
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    return repo_dir


def _tree(tmp_path):
    return _repo(tmp_path, {
        "Makefile": "replay:\n\techo build\n",
        "src/mod_params.f90": "module mod_params\nend module\n",
        "src/mod_kernel.f90": "module mod_kernel\nend module\n",
        "README.md": "how this code works\n",
    })


def _check(tmp_path, builder, repo_dir=None):
    return build_replay.check(
        repo_dir or _tree(tmp_path), "main", "ch04:step", "tree123",
        load_strategy(STRATEGY_PATH), _manifest(tmp_path), builder,
    )


def test_the_whole_tree_goes_to_the_builder_not_a_filtered_source_list(tmp_path):
    # The build is the tree's own makefile, and a makefile reads files no
    # extension test would call source.
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    assert result["verdict"] == "pass"
    sent = {f["path"] for f in builder.build_calls[0]["tree"]}
    assert sent == {"Makefile", "src/mod_params.f90", "src/mod_kernel.f90", "README.md"}
    assert base64.b64decode(
        next(f["b64"] for f in builder.build_calls[0]["tree"] if f["path"] == "README.md")
    ) == b"how this code works\n"


def test_the_build_recipe_comes_from_the_manifest_and_the_flags_from_the_strategy(tmp_path):
    strategy = load_strategy(STRATEGY_PATH)
    manifest = _manifest(tmp_path)
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    call = builder.build_calls[0]
    assert call["makefile"] == manifest.build.makefile
    # Every target the code declares, so a tree that stopped building its
    # own program says so here rather than three actions later.
    assert call["targets"] == [
        {"role": "replay", "target": "replay", "executable": "replay"},
        {"role": "timing", "target": "timing", "executable": "whole_program"},
        {"role": "capture", "target": "capture", "executable": "gen_reference"},
    ]
    assert call["compiler"] == strategy.languages["fortran"].compiler
    assert call["flags"] == list(strategy.languages["fortran"].flags)
    assert call["link_flags"] == list(strategy.link_flags)
    assert call["source_patterns"] == list(manifest.source.patterns)
    assert result["detail"]["flags"] == list(strategy.languages["fortran"].flags)


def test_a_pass_records_every_compile_the_builder_saw(tmp_path):
    builder = FakeBuilder()

    result = _check(tmp_path, builder)

    assert result["detail"]["compiles"][0]["inputs"] == ["src/mod_kernel.f90"]
    assert result["detail"]["targets"]["replay"]["built"] is True


def test_fail_when_the_builder_reports_a_compile_error(tmp_path):
    builder = FakeBuilder()
    builder.build_ok = False

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert "log_tail" in result["detail"]


def test_a_build_whose_flags_never_reached_the_compiler_fails_naming_the_command(tmp_path):
    # The makefile set its own FFLAGS. It compiled, it linked, and what
    # ran on the GPU was not what the strategy says was measured.
    builder = FakeBuilder()
    builder.flags_reached = False

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["compiles_without_flags"] == [builder.build_calls[0]["flags"] + [
        "-o", "replay", "src/mod_kernel.f90",
    ]]


def test_a_build_that_compiled_a_file_from_outside_the_tree_fails_naming_the_file(tmp_path):
    builder = FakeBuilder()
    builder.only_tree_source = False

    result = _check(tmp_path, builder)

    assert result["verdict"] == "fail"
    assert result["detail"]["files_outside_tree"] == [builder.outside_file]


def test_raises_component_error_when_the_tree_holds_no_source(tmp_path):
    repo_dir = _repo(tmp_path, {"README.md": "hello\n"})

    with pytest.raises(ComponentError):
        _check(tmp_path, FakeBuilder(), repo_dir=repo_dir)
