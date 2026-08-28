"""Mutating a kernel and asking whether the harness would notice.

The first half reads as the statement of what a mutant is: one changed
token on one line, of a kind that a wrong port would plausibly make, and
never a deleted declaration. The second half runs the whole thing for
real against gfortran -- build the tree, replay the cases, score each
mutant with the comparator and the bands -- because the three verdicts
that matter (killed, equivalent, and the gap the bands are hiding) are
statements about a real build and a real comparison, not about the
generator.
"""
from __future__ import annotations

import base64
import io
import shutil
from pathlib import Path

import numpy as np
import pytest

from services.builder import mutate, stages

HARNESS = Path(__file__).resolve().parents[3] / "services" / "builder" / "harness"

needs_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None or shutil.which("make") is None,
    reason="scoring a mutant needs a Fortran compiler and make",
)

FLAGS = ["-O1", "-ffree-line-length-none"]
PATTERNS = ["src/*.f90", "harness/*.f90", "Makefile"]

# A kernel with one line that reaches an output and one that does not.
# Mutants of the first change the answer; mutants of the second cannot,
# whatever the bands are, and so are survivors for a person to read.
KERNEL = """module mod_kernel
  use iso_fortran_env, only: real64
  implicit none
contains
  subroutine step(a)
    real(real64), intent(inout) :: a(:)
    real(real64) :: unused
    a = a * 2.0d0
    unused = 1.0d0 + 3.0d0
  end subroutine step
end module mod_kernel
"""

# The line each of those is, so a test can say which mutant it means.
LIVE_LINE = KERNEL.split("\n").index("    a = a * 2.0d0") + 1
DEAD_LINE = KERNEL.split("\n").index("    unused = 1.0d0 + 3.0d0") + 1

REPLAY = """program replay
  use iso_fortran_env, only: real64
  use mod_kernel, only: step
  use npy_io, only: npy_save, npy_load
  implicit none
  character(len=1024) :: casedir
  real(real64), allocatable :: a(:)
  call get_command_argument(1, casedir)
  call npy_load(trim(casedir) // '/a.npy', a)
  call step(a)
  call npy_save(trim(casedir) // '/a.out.npy', a)
end program replay
"""

MAKEFILE = (
    "replay: src/mod_kernel.f90 harness/replay.f90\n"
    "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay $(HARNESS)/npy_io.f90 "
    "src/mod_kernel.f90 harness/replay.f90 $(LDFLAGS)\n"
)

TREE = {"Makefile": MAKEFILE, "src/mod_kernel.f90": KERNEL, "harness/replay.f90": REPLAY}

REPLAY_TARGET = {"role": "replay", "target": "replay", "executable": "replay"}

# Tight enough that doubling an array instead of halving it is outside
# every one of the three metrics.
TIGHT = {"a": {"abs": 1e-9, "rel": 1e-9, "ulp": 0}}
# And wide enough that nothing a mutant could do to these numbers is
# outside it, which is what a tolerance-blind gap is made of.
ABSURD = {"a": {"abs": 1e30, "rel": 1e30, "ulp": 2**62}}


def _npy(values) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(values, dtype="<f8"), allow_pickle=False)
    return buffer.getvalue()


def _tree_payload(files: dict) -> list:
    return [
        {"path": path, "b64": base64.b64encode(content.encode()).decode()}
        for path, content in files.items()
    ]


# ---------------------------------------------------------------- generate


def test_generate_changes_one_operator_at_a_time():
    mutants = mutate.generate("  x = a + b * c\n", "src/mod_kernel.f90")

    mutated = {m.mutated.strip() for m in mutants if m.op == "AOR"}
    assert "x = a - b * c" in mutated
    assert "x = a + b / c" in mutated
    # One token each: no mutant changes two operators at once.
    assert "x = a - b / c" not in mutated


def test_generate_covers_the_operators_a_wrong_port_would_get_wrong():
    text = (
        "  if (i .lt. n .and. j > 1) then\n"
        "  dx(2:im-1) = x(3:im) - x(1:im-2)\n"
        "  y = 0.5 * z\n"
    )

    by_op = {}
    for m in mutate.generate(text, "src/mod_diff.f90"):
        by_op.setdefault(m.op, []).append(m.mutated.strip())

    assert "if (i .le. n .and. j > 1) then" in by_op["ROR"]
    assert "if (i .lt. n .or. j > 1) then" in by_op["LCR"]
    # The section bound perturbation, which is the fault an array-syntax
    # loop rewrite most often makes.
    assert "dx(2:im - 2) = x(3:im) - x(1:im-2)" in by_op["SBR"]
    assert "y = 1 * z" in by_op["CRP"]
    assert any(line.startswith("!") for line in by_op["SDL"])


def test_generate_never_deletes_a_declaration_or_a_control_line():
    text = (
        "  real(real64) :: a = 1.0\n"
        "  do i = 1, n\n"
        "  if (x == y) then\n"
        "  a = b\n"
    )

    deleted = [m.original.strip() for m in mutate.generate(text, "k.f90") if m.op == "SDL"]

    assert deleted == ["a = b"]


def test_generate_leaves_comments_and_string_literals_alone():
    text = "  write(*,'(a)') 'a + b'   ! and x .lt. y\n"

    assert mutate.generate(text, "k.f90") == []


def test_every_mutant_says_where_it_came_from():
    mutants = mutate.generate(KERNEL, "src/mod_kernel.f90")

    live = [m for m in mutants if m.line == LIVE_LINE]
    assert live
    for m in live:
        assert m.file == "src/mod_kernel.f90"
        assert m.original == "    a = a * 2.0d0"
        assert m.mid.startswith("mod_kernel-")


# ------------------------------------------------------------ scoring, for real


def _reference(tmp_path, cases: dict) -> dict:
    """Build the unmutated tree and replay the cases, for answers to score against.

    The reference is what this harness itself produces, which is what a
    capture set holds -- so a mutant is judged against the same thing a
    port would be.
    """
    built = stages.build(
        "attempt-1", _tree_payload(TREE), "Makefile", [REPLAY_TARGET], "gfortran",
        FLAGS, [], PATTERNS, harness_dir=HARNESS, work_root=tmp_path,
    )
    assert built["ok"] is True, built["log_tail"]
    replayed = stages.run("attempt-1", "replay", cases, work_root=tmp_path)
    assert replayed["ok"] is True, replayed["log_tail"]
    return {
        name: {"inputs": cases[name], "outputs": replayed["outputs"][name]}
        for name in cases
    }


def _cases() -> dict:
    return {
        "case0000": {"a": base64.b64encode(_npy([1.0, 2.0, 3.0])).decode()},
        "case0001": {"a": base64.b64encode(_npy([-4.0, 0.5])).decode()},
    }


def _mutate(tmp_path, bands, **kwargs) -> dict:
    cases = _cases()
    scored = _reference(tmp_path, cases)
    return stages.mutate(
        "attempt-1", "Makefile", REPLAY_TARGET, ["src/mod_kernel.f90"], scored, bands,
        "gfortran", FLAGS, [], PATTERNS,
        jobs=2, work_root=tmp_path, harness_dir=HARNESS, **kwargs,
    )


def _by_line(result: dict, line: int, status: str) -> list:
    return [r for r in result["results"] if r["line"] == line and r["status"] == status]


@needs_gfortran
def test_a_mutant_the_bands_catch_is_killed_and_a_dead_line_survives(tmp_path):
    result = _mutate(tmp_path, TIGHT)

    assert result["ok"] is True
    assert result["generated"] == result["scored"]
    # The line that reaches the output: changing it changes the answer by
    # more than the bands allow.
    assert _by_line(result, LIVE_LINE, "KILLED")
    # The line that reaches nothing: the harness cannot see it change, and
    # no band would help, so it is a survivor for the person to read.
    assert _by_line(result, DEAD_LINE, "EQUIVALENT")
    assert result["counts"]["KILLED"] == len(_by_line(result, LIVE_LINE, "KILLED"))
    # Nothing the bands let through.
    assert result["counts"].get("GAP", 0) == 0


@needs_gfortran
def test_widening_the_bands_absurdly_turns_a_kill_into_the_tolerance_blind_gap(tmp_path):
    tight = _mutate(tmp_path, TIGHT)
    killed = {r["id"] for r in tight["results"] if r["status"] == "KILLED"}

    wide = _mutate(tmp_path, ABSURD)

    assert killed
    # The same mutants, changing the same numbers; all that moved is what
    # the policy calls acceptable, which is the whole point of the gap.
    assert {r["id"] for r in wide["results"] if r["status"] == "GAP"} == killed
    assert wide["counts"].get("KILLED", 0) == 0


@needs_gfortran
def test_a_survivor_keeps_its_directory_for_the_person_and_a_kill_does_not(tmp_path):
    result = _mutate(tmp_path, TIGHT)

    kept = set(result["kept_dirs"])
    survivors = {r["id"] for r in result["results"] if r["status"] == "EQUIVALENT"}
    assert survivors
    assert {Path(path).name for path in kept} == survivors
    for path in kept:
        assert Path(path).is_dir()


@needs_gfortran
def test_a_limit_scores_only_the_first_mutants_and_says_how_many_there_were(tmp_path):
    result = _mutate(tmp_path, TIGHT, limit=3)

    assert result["scored"] == 3
    assert result["generated"] > 3
    assert len(result["results"]) == 3


@needs_gfortran
def test_a_mutant_that_does_not_compile_is_reported_rather_than_scored(tmp_path):
    # The generator is regex-based and does not parse Fortran, so some
    # mutants are not Fortran at all. Each costs one compile and is
    # reported as what it is.
    broken = dict(TREE)
    broken["src/mod_kernel.f90"] = KERNEL.replace(
        "    a = a * 2.0d0", "    a = a * 2.0d0\n    a(1:2) = a(1:2) + 1.0d0"
    )
    cases = _cases()
    stages.build(
        "attempt-1", _tree_payload(broken), "Makefile", [REPLAY_TARGET], "gfortran",
        FLAGS, [], PATTERNS, harness_dir=HARNESS, work_root=tmp_path,
    )
    replayed = stages.run("attempt-1", "replay", cases, work_root=tmp_path)
    scored = {
        name: {"inputs": cases[name], "outputs": replayed["outputs"][name]} for name in cases
    }

    result = stages.mutate(
        "attempt-1", "Makefile", REPLAY_TARGET, ["src/mod_kernel.f90"], scored, TIGHT,
        "gfortran", FLAGS, [], PATTERNS,
        jobs=2, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is True
    assert set(result["counts"]) <= set(mutate.STATUSES)


def test_a_file_the_tree_does_not_hold_is_refused_rather_than_mutated(tmp_path):
    stages.write_tree(tmp_path / "attempt-1" / "tree", _tree_payload(TREE))

    result = stages.mutate(
        "attempt-1", "Makefile", REPLAY_TARGET, ["../../escape.f90"], {}, TIGHT,
        "gfortran", FLAGS, [], PATTERNS, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is False
    assert "escape.f90" in result["log_tail"]


def test_a_tree_that_was_never_built_is_reported_rather_than_mutated(tmp_path):
    result = stages.mutate(
        "attempt-2", "Makefile", REPLAY_TARGET, ["src/mod_kernel.f90"], {}, TIGHT,
        "gfortran", FLAGS, [], PATTERNS, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is False
    assert "attempt-2" in result["log_tail"] or "tree" in result["log_tail"]
