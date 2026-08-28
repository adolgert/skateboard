from pathlib import Path

from equivalent.components import sese_check
from equivalent.gateway.submit import init_baseline_repo
from equivalent.strategy.schema import load_strategy

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"

CLEAN_SOURCE = """\
module mod_kernel
contains
subroutine step(h, u)
  real :: h, u
  h = h + u
end subroutine step
end module mod_kernel
"""

GOTO_SOURCE = """\
module mod_kernel
contains
subroutine step(h, u)
  real :: h, u
  if (h > 0) goto 10
  h = h + u
10 continue
end subroutine step
end module mod_kernel
"""


def _spec(anchor_file, hi):
    return (
        "region: ch04:step\n"
        "anchor:\n"
        f"  file: {anchor_file}\n"
        f'  pst_node: "step@3-{hi}"\n'
        "  entry_symbol: step\n"
    )


def _seed(tmp_path, source, anchor_file, hi):
    (tmp_path / Path(anchor_file).parent).mkdir(parents=True, exist_ok=True)
    (tmp_path / anchor_file).write_text(source)
    (tmp_path / "notes" / "regions").mkdir(parents=True)
    (tmp_path / SPEC_PATH).write_text(_spec(anchor_file, hi))
    return tmp_path


def _repo(tmp_path, source, anchor_file="src/mod_kernel.f90", hi=5):
    seed = _seed(tmp_path / "seed", source, anchor_file, hi)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    return repo_dir


def test_clean_region_passes_and_reports_file_list_and_allow_globs(tmp_path):
    repo_dir = _repo(tmp_path, CLEAN_SOURCE)
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "pass"
    assert result["detail"]["file_list"] == ["src/mod_kernel.f90"]
    assert result["allow_globs"] == sorted(["src/mod_kernel.f90", SPEC_PATH])
    assert result["detail"]["allow_globs"] == result["allow_globs"]


def test_region_with_goto_fails_and_names_the_violation(tmp_path):
    repo_dir = _repo(tmp_path, GOTO_SOURCE, hi=8)
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "fail"
    assert result["allow_globs"] is None
    [violation] = result["detail"]["violations"]
    assert violation["keyword"] == "goto"
    assert violation["line"] == 5


def test_anchor_outside_the_strategys_allow_globs_fails_without_a_pass(tmp_path):
    # lib/*.f90 isn't covered by stdpar_managed's allow_globs (src/*.f90),
    # even though the code itself is clean SESE control flow.
    repo_dir = _repo(tmp_path, CLEAN_SOURCE, anchor_file="lib/mod_kernel.f90")
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "fail"
    assert result["allow_globs"] is None
    assert "lib/mod_kernel.f90" in result["detail"]["paths"]


def test_check_reads_the_committed_tree_not_whatever_is_on_disk_in_repo_dir(tmp_path):
    # Step 4 never checks out a region's branch into repo_dir's working
    # tree, so stray on-disk content there must not affect the result --
    # only what was actually committed to the ref being checked.
    repo_dir = _repo(tmp_path, CLEAN_SOURCE)
    (repo_dir / "src" / "mod_kernel.f90").write_text(GOTO_SOURCE)
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "pass"
