from pathlib import Path

from equivalent.components import sese_check
from equivalent.gateway.submit import init_baseline_repo
from equivalent.strategy.schema import load_strategy

STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"

CLEAN_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  a = a + b
end subroutine step
end module mod_kernel
"""

GOTO_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  if (h > 0) goto 10
  a = a + b
10 continue
end subroutine step
end module mod_kernel
"""

DIFF_SOURCE = """\
module mod_diff
contains
pure function diff(x) result(dx)
  real :: x(:), dx(size(x))
  dx = x
end function diff
end module mod_diff
"""


def _spec(files, anchor_file, hi, callee_file=None):
    listed = "".join(f"  - {f}\n" for f in files)
    text = (
        "region: ch04:step\n"
        "files:\n"
        f"{listed}"
        "anchor:\n"
        f"  file: {anchor_file}\n"
        f'  pst_node: "step@3-{hi}"\n'
        "  entry_symbol: step\n"
    )
    if callee_file is not None:
        text += (
            "closure:\n"
            "  callees:\n"
            "    - name: diff\n"
            f"      file: {callee_file}\n"
            '      lines: "3-6"\n'
        )
    return text


def _repo(tmp_path, spec_text, sources):
    seed = tmp_path / "seed"
    for path, source in sources.items():
        (seed / path).parent.mkdir(parents=True, exist_ok=True)
        (seed / path).write_text(source)
    (seed / "notes" / "regions").mkdir(parents=True)
    (seed / SPEC_PATH).write_text(spec_text)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    return repo_dir


def _one_file_repo(tmp_path, source, anchor_file="src/mod_kernel.f90", hi=5):
    return _repo(tmp_path, _spec([anchor_file], anchor_file, hi), {anchor_file: source})


def test_clean_region_passes_and_reports_file_list_and_allow_globs(tmp_path):
    repo_dir = _one_file_repo(tmp_path, CLEAN_SOURCE)
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "pass"
    assert result["detail"]["file_list"] == ["src/mod_kernel.f90"]
    assert result["allow_globs"] == sorted(["src/mod_kernel.f90", SPEC_PATH])
    assert result["detail"]["allow_globs"] == result["allow_globs"]


def test_a_region_spanning_two_files_unfreezes_both_of_them_and_the_spec(tmp_path):
    files = ["src/mod_kernel.f90", "src/mod_diff.f90"]
    repo_dir = _repo(
        tmp_path,
        _spec(files, "src/mod_kernel.f90", 5, callee_file="src/mod_diff.f90"),
        {"src/mod_kernel.f90": CLEAN_SOURCE, "src/mod_diff.f90": DIFF_SOURCE},
    )
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "pass"
    assert result["detail"]["file_list"] == sorted(files)
    assert result["allow_globs"] == sorted([*files, SPEC_PATH])


def test_region_with_goto_fails_and_names_the_violation(tmp_path):
    repo_dir = _one_file_repo(tmp_path, GOTO_SOURCE, hi=8)
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
    repo_dir = _one_file_repo(tmp_path, CLEAN_SOURCE, anchor_file="lib/mod_kernel.f90")
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "fail"
    assert result["allow_globs"] is None
    assert "lib/mod_kernel.f90" in result["detail"]["paths"]


def test_a_second_listed_file_outside_the_allow_globs_fails_and_names_that_file(tmp_path):
    # The anchor is where the strategy permits and the control flow is
    # clean, so only the second file is the reason -- and the answer says
    # which one it is.
    repo_dir = _repo(
        tmp_path,
        _spec(["src/mod_kernel.f90", "lib/mod_diff.f90"], "src/mod_kernel.f90", 5),
        {"src/mod_kernel.f90": CLEAN_SOURCE, "lib/mod_diff.f90": DIFF_SOURCE},
    )
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "fail"
    assert result["allow_globs"] is None
    assert result["detail"]["paths"] == ["lib/mod_diff.f90"]


def test_a_spec_that_lists_no_files_is_a_failed_verdict(tmp_path):
    # A malformed spec comes back as a verdict the gateway can record, not
    # as an infrastructure error with nowhere to put it.
    spec_text = (
        "region: ch04:step\n"
        "anchor:\n"
        "  file: src/mod_kernel.f90\n"
        '  pst_node: "step@3-5"\n'
        "  entry_symbol: step\n"
    )
    repo_dir = _repo(tmp_path, spec_text, {"src/mod_kernel.f90": CLEAN_SOURCE})
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "fail"
    assert result["allow_globs"] is None
    [violation] = result["detail"]["violations"]
    assert "files" in violation["reason"]


def test_check_reads_the_committed_tree_not_whatever_is_on_disk_in_repo_dir(tmp_path):
    # submit() never checks out a region's branch into repo_dir's working
    # tree, so stray on-disk content there must not affect the result --
    # only what was actually committed to the ref being checked.
    repo_dir = _one_file_repo(tmp_path, CLEAN_SOURCE)
    (repo_dir / "src" / "mod_kernel.f90").write_text(GOTO_SOURCE)
    strategy = load_strategy(STRATEGY_PATH)

    result = sese_check.check(repo_dir, "main", SPEC_PATH, strategy)

    assert result["verdict"] == "pass"
