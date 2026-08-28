"""The builder's stages, run for real against gfortran where one is present.

These are the tests that actually put a Makefile in front of `make` and
read the log the shim wrote, because that pairing is the whole point of
the build contract: the tree says how to build itself, and the log is
what says whether it obeyed. They are skipped where no gfortran is
installed, so the suite still runs on a machine with no compiler.
"""
import base64
import shutil
from pathlib import Path

import pytest

from services.builder import stages

HARNESS = Path(__file__).resolve().parents[3] / "services" / "builder" / "harness"

needs_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None or shutil.which("make") is None,
    reason="the build contract needs a Fortran compiler and make",
)

# The flags the strategy asks for. -O1 and -fno-range-check are chosen
# because gfortran accepts both and neither is a default, so a Makefile
# that ignores FFLAGS cannot pass by accident.
FLAGS = ["-O1", "-fno-range-check"]
PATTERNS = ["src/*.f90", "*.f90", "Makefile"]

MAIN = """program main
  print *, 'ran'
end program main
"""


def tree_of(files: dict) -> list:
    return [
        {"path": path, "b64": base64.b64encode(content.encode()).decode()}
        for path, content in files.items()
    ]


def build(tmp_path, files, targets=None, compiler="gfortran"):
    return stages.build(
        "attempt-1", tree_of(files), "Makefile",
        targets or [{"role": "replay", "target": "replay", "executable": "replay"}],
        compiler, FLAGS, [], PATTERNS,
        harness_dir=HARNESS, work_root=tmp_path,
    )


def test_the_tree_is_written_with_its_directories_intact(tmp_path):
    stages.write_tree(tmp_path / "tree", tree_of({
        "Makefile": "all:\n", "src/deep/mod_kernel.f90": MAIN,
    }))

    assert (tmp_path / "tree" / "src" / "deep" / "mod_kernel.f90").read_text() == MAIN


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape.f90", "src/../../escape.f90"])
def test_a_path_that_would_leave_the_workspace_is_refused_by_name(tmp_path, path):
    with pytest.raises(ValueError) as excinfo:
        stages.write_tree(tmp_path / "tree", tree_of({path: MAIN}))

    assert path in str(excinfo.value)


@needs_gfortran
def test_a_build_that_obeys_the_makefile_reports_every_compile(tmp_path):
    result = build(tmp_path, {
        "Makefile": (
            "replay: src/main.f90\n"
            "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay src/main.f90 $(LDFLAGS)\n"
        ),
        "src/main.f90": MAIN,
    })

    assert result["ok"] is True
    assert result["targets"]["replay"] == {"executable": "replay", "built": True}
    assert result["flags_reached_every_compile"] is True
    assert result["compiled_only_tree_source"] is True
    assert [record["inputs"] for record in result["compiles"]] == [["src/main.f90"]]
    assert result["compiles"][0]["output"] == "replay"


@needs_gfortran
def test_a_makefile_that_hard_codes_its_flags_names_the_compile_that_ignored_them(tmp_path):
    result = build(tmp_path, {
        "Makefile": (
            "FFLAGS = -O0\n"
            "replay: src/main.f90\n"
            "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay src/main.f90\n"
        ),
        "src/main.f90": MAIN,
    })

    # The build succeeded. Only the log says the strategy never reached it.
    assert result["targets"]["replay"]["built"] is True
    assert result["flags_reached_every_compile"] is False
    assert result["compiles"][0]["has_flags"] is False
    assert "-O0" in result["compiles"][0]["argv"]


@needs_gfortran
def test_a_build_that_compiles_a_file_from_outside_the_tree_names_it(tmp_path):
    result = build(tmp_path, {
        "Makefile": (
            "replay: src/main.f90\n"
            "\tprintf 'module extra\\nend module extra\\n' > ../extra.f90\n"
            "\t$(FC) $(FFLAGS) $(MODFLAG) . -c ../extra.f90\n"
            "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay src/main.f90 extra.o\n"
        ),
        "src/main.f90": MAIN,
    })

    assert result["compiled_only_tree_source"] is False
    outside = [path for record in result["compiles"] for path in record["outside"]]
    assert [path.endswith("extra.f90") for path in outside] == [True]


@needs_gfortran
def test_two_sources_with_the_same_basename_in_different_directories_both_build(tmp_path):
    same_name = "module {name}\ncontains\n  subroutine {name}_hello\n  end subroutine\nend module\n"
    result = build(tmp_path, {
        "Makefile": (
            "SRC = src/a/x.f90 src/b/x.f90 src/main.f90\n"
            "replay: $(SRC)\n"
            "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay $(SRC)\n"
        ),
        "src/a/x.f90": same_name.format(name="alpha"),
        "src/b/x.f90": same_name.format(name="beta"),
        "src/main.f90": "program main\n  use alpha\n  use beta\nend program\n",
    })

    assert result["ok"] is True
    assert result["compiles"][0]["inputs"] == ["src/a/x.f90", "src/b/x.f90", "src/main.f90"]


@needs_gfortran
def test_every_declared_target_is_built_and_reported_by_its_role(tmp_path):
    result = build(
        tmp_path,
        {
            "Makefile": (
                "replay: src/main.f90\n"
                "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay src/main.f90\n"
                "timing: whole_program\n"
                "whole_program: src/main.f90\n"
                "\t$(FC) $(FFLAGS) $(MODFLAG) . -o whole_program src/main.f90\n"
                ".PHONY: timing\n"
            ),
            "src/main.f90": MAIN,
        },
        targets=[
            {"role": "replay", "target": "replay", "executable": "replay"},
            {"role": "timing", "target": "timing", "executable": "whole_program"},
        ],
    )

    assert result["ok"] is True
    assert sorted(result["targets"]) == ["replay", "timing"]
    assert result["targets"]["timing"]["executable"] == "whole_program"


@needs_gfortran
def test_a_target_that_leaves_no_executable_fails_naming_the_role(tmp_path):
    result = build(
        tmp_path,
        {"Makefile": "replay:\n\t@echo nothing to do here\n.PHONY: replay\n"},
        targets=[{"role": "replay", "target": "replay", "executable": "replay"}],
    )

    assert result["ok"] is False
    assert "replay" in result["log_tail"]
    assert result["targets"]["replay"]["built"] is False


@needs_gfortran
def test_a_compile_error_is_a_failed_build_carrying_the_compiler_log(tmp_path):
    result = build(tmp_path, {
        "Makefile": "replay: src/main.f90\n\t$(FC) $(FFLAGS) -o replay src/main.f90\n",
        "src/main.f90": "program main\n  this is not fortran(\nend program\n",
    })

    assert result["ok"] is False
    assert result["stage"] == "build"
    assert "Error" in result["log_tail"] or "error" in result["log_tail"]


# A replay driver small enough to read: it writes one output file into the
# case directory it is given. What is in that file does not matter here --
# the builder never looks inside one; the oracle does.
WRITER = """program writer
  character(len=512) :: casedir
  call get_command_argument(1, casedir)
  open(unit=10, file=trim(casedir)//'/field.out.npy', access='stream', form='unformatted')
  write(10) 'OUT'
  close(10)
end program writer
"""

WRITER_MAKEFILE = (
    "replay: src/writer.f90\n"
    "\t$(FC) $(FFLAGS) $(MODFLAG) . -o replay src/writer.f90\n"
)


@needs_gfortran
def test_run_writes_the_inputs_replays_each_case_and_collects_the_outputs(tmp_path):
    build(tmp_path, {"Makefile": WRITER_MAKEFILE, "src/writer.f90": WRITER})
    cases = {"case0000": {"field": base64.b64encode(b"IN").decode()}}

    result = stages.run("attempt-1", "replay", cases, work_root=tmp_path)

    assert result["ok"] is True
    assert base64.b64decode(result["outputs"]["case0000"]["field"]) == b"OUT"
    # The inputs really were laid out as files for the driver to read.
    assert (tmp_path / "attempt-1" / "cases" / "case0000" / "field.npy").read_bytes() == b"IN"


@needs_gfortran
def test_run_reports_the_executable_the_manifest_named_when_it_is_missing(tmp_path):
    build(tmp_path, {"Makefile": WRITER_MAKEFILE, "src/writer.f90": WRITER})

    result = stages.run("attempt-1", "some_other_binary", {"case0000": {}}, work_root=tmp_path)

    assert result["ok"] is False
    assert "some_other_binary" in result["log_tail"]


TIMER = """program timer
  open(unit=10, file='result.dat')
  write(10, *) 'done'
  close(10)
end program timer
"""

TIMER_MAKEFILE = (
    "timing: whole_program\n"
    "whole_program: src/timer.f90\n"
    "\t$(FC) $(FFLAGS) $(MODFLAG) . -o whole_program src/timer.f90\n"
    ".PHONY: timing\n"
)


def _timing_tree(tmp_path):
    return build(
        tmp_path,
        {"Makefile": TIMER_MAKEFILE, "src/timer.f90": TIMER},
        targets=[{"role": "timing", "target": "timing", "executable": "whole_program"}],
    )


@needs_gfortran
def test_time_run_repeats_the_program_and_returns_the_files_it_declared(tmp_path):
    _timing_tree(tmp_path)

    result = stages.time_run(
        "attempt-1", "whole_program", args=[], env={"EQUIVALENT_TEST": "1"},
        outputs=["result.dat"], repeats=2, budget_s=60, work_root=tmp_path,
    )

    assert result["ok"] is True
    assert len(result["runs_s"]) == 2
    assert b"done" in base64.b64decode(result["outputs"]["result.dat"])


@needs_gfortran
def test_time_run_fails_naming_an_output_file_the_program_did_not_write(tmp_path):
    _timing_tree(tmp_path)

    result = stages.time_run(
        "attempt-1", "whole_program", args=[], env={}, outputs=["result.dat", "energy.csv"],
        repeats=1, budget_s=60, work_root=tmp_path,
    )

    assert result["ok"] is False
    assert "energy.csv" in result["log_tail"]


@needs_gfortran
def test_time_run_clears_a_declared_output_left_by_an_earlier_run(tmp_path):
    # Otherwise a program that stopped writing its output would be timed
    # happily and compared against the file the last port left behind.
    _timing_tree(tmp_path)
    stale = tmp_path / "attempt-1" / "tree" / "energy.csv"
    stale.write_text("from an earlier attempt\n")

    result = stages.time_run(
        "attempt-1", "whole_program", args=[], env={}, outputs=["energy.csv"],
        repeats=1, budget_s=60, work_root=tmp_path,
    )

    assert result["ok"] is False
    assert not stale.exists()
