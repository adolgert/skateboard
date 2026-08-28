from pathlib import Path

from equivalent.components import timing
from equivalent.gateway.submit import init_baseline_repo
from equivalent.tests.fakes import FakeBuilder


def test_port_pass_reports_the_measured_runs():
    builder = FakeBuilder()

    result = timing.check_port("ch04:step", "tree123", builder)

    assert result["verdict"] == "pass"
    assert result["detail"]["runs_s"] == builder.runs_s


def test_port_fail_when_the_binary_is_not_built():
    builder = FakeBuilder()
    builder.time_ok = False

    result = timing.check_port("ch04:step", "tree123", builder)

    assert result["verdict"] == "fail"


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
