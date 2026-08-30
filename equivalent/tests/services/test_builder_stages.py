"""The builder's stages, run for real against gfortran where one is present.

These are the tests that actually put a Makefile in front of `make` and
read the log the shim wrote, because that pairing is the whole point of
the build contract: the tree says how to build itself, and the log is
what says whether it obeyed. They are skipped where no gfortran is
installed, so the suite still runs on a machine with no compiler.
"""
import base64
import importlib.util
import io
import shutil
from pathlib import Path

import numpy as np
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
    # One set of collected files per run, so a caller can ask whether the
    # program wrote the same thing both times.
    assert len(result["outputs"]) == 2
    assert b"done" in base64.b64decode(result["outputs"][-1]["result.dat"])


# A program whose declared output really is different every run. It counts
# its own runs in a file beside the output rather than reading a clock:
# two runs a few milliseconds apart can land on the same tick, and then
# the test below would fail for a reason that has nothing to do with what
# it is asking.
DRIFTING_TIMER = """program drifting
  integer :: n
  logical :: there
  n = 0
  inquire(file='runs.dat', exist=there)
  if (there) then
    open(unit=11, file='runs.dat', status='old')
    read(11, *) n
    close(11)
  end if
  n = n + 1
  open(unit=11, file='runs.dat', status='replace')
  write(11, *) n
  close(11)
  open(unit=10, file='result.dat')
  write(10, *) n
  close(10)
end program drifting
"""


@needs_gfortran
def test_time_run_collects_each_runs_own_files_so_a_drifting_output_is_visible(tmp_path):
    # A program whose declared output changes from run to run is the thing
    # a caller most wants to know about, and it is invisible if the files
    # are only collected once at the end.
    build(
        tmp_path,
        {"Makefile": TIMER_MAKEFILE.replace("src/timer.f90", "src/drifting.f90"),
         "src/drifting.f90": DRIFTING_TIMER},
        targets=[{"role": "timing", "target": "timing", "executable": "whole_program"}],
    )

    result = stages.time_run(
        "attempt-1", "whole_program", args=[], env={}, outputs=["result.dat"],
        repeats=2, budget_s=60, work_root=tmp_path,
    )

    assert result["ok"] is True
    assert result["outputs"][0]["result.dat"] != result["outputs"][1]["result.dat"]


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


# A capture program small enough to read: it takes a number of cases and,
# as every capture program does, the directory to write them into as its
# last argument. It writes real NPY files, because the capture format is
# the contract and a test that wrote its own bytes would not be checking
# it. A shell script rather than Fortran, so this runs where no compiler
# is installed.
CAPTURE_PROGRAM = """#!/bin/sh
set -e
"PYTHON" - "$1" "$2" <<'PYEOF'
import json, sys
import numpy as np
from pathlib import Path
count, outdir = int(sys.argv[1]), Path(sys.argv[2])
for i in range(count):
    case = outdir / ("case%04d" % i)
    case.mkdir(parents=True)
    np.save(case / "h.npy", np.asarray([i, i + 1], dtype="<f4"))
    np.save(case / "h.out.npy", np.asarray([i + 2, i + 3], dtype="<f4"))
    (case / "case.json").write_text(json.dumps({"inputs": ["h"], "outputs": ["h"]}))
print("wrote", count, "cases")
PYEOF
"""


def _capture_tree(tmp_path, program=None):
    """A workspace holding one executable capture program and nothing else."""
    import os
    import sys
    source = CAPTURE_PROGRAM.replace("PYTHON", sys.executable) if program is None else program
    tree_dir = stages.write_tree(
        tmp_path / "attempt-1" / "tree", tree_of({"gen_reference": source}),
    )
    os.chmod(Path(tree_dir) / "gen_reference", 0o755)
    return tree_dir


def test_capture_returns_every_case_directory_the_program_wrote(tmp_path):
    _capture_tree(tmp_path)

    result = stages.capture(
        "attempt-1", "gen_reference", ["2"], "visible", work_root=tmp_path,
    )

    assert result["ok"] is True
    assert sorted(result["cases"]) == ["case0000", "case0001"]
    case = result["cases"]["case0000"]
    assert sorted(case["inputs"]) == ["h"] and sorted(case["outputs"]) == ["h"]
    assert base64.b64decode(case["inputs"]["h"]).startswith(b"\x93NUMPY")
    assert "wrote 2 cases" in result["stdout_tail"]


def test_capture_writes_into_a_directory_named_for_the_run(tmp_path):
    _capture_tree(tmp_path)

    stages.capture("attempt-1", "gen_reference", ["1"], "holdout", work_root=tmp_path)

    assert (tmp_path / "attempt-1" / "captures" / "holdout" / "case0000").is_dir()


def test_capture_starts_from_an_empty_directory_each_time(tmp_path):
    # Otherwise a run that captured fewer cases than the last one would
    # come back holding cases the program did not write this time.
    _capture_tree(tmp_path)

    stages.capture("attempt-1", "gen_reference", ["3"], "visible", work_root=tmp_path)
    result = stages.capture("attempt-1", "gen_reference", ["1"], "visible", work_root=tmp_path)

    assert sorted(result["cases"]) == ["case0000"]


def test_capture_that_leaves_no_case_directory_says_so(tmp_path):
    _capture_tree(tmp_path, program='#!/bin/sh\necho "nothing to capture"\n')

    result = stages.capture("attempt-1", "gen_reference", [], "visible", work_root=tmp_path)

    assert result["ok"] is False
    assert "no case" in result["stdout_tail"]
    assert result["cases"] == {}


def test_capture_reports_a_program_that_failed(tmp_path):
    _capture_tree(tmp_path, program='#!/bin/sh\necho "bad grid size" >&2\nexit 2\n')

    result = stages.capture("attempt-1", "gen_reference", [], "visible", work_root=tmp_path)

    assert result["ok"] is False
    assert "bad grid size" in result["stdout_tail"]


def test_capture_reports_the_executable_the_manifest_named_when_it_is_missing(tmp_path):
    _capture_tree(tmp_path)

    result = stages.capture("attempt-1", "no_such_program", [], "visible", work_root=tmp_path)

    assert result["ok"] is False
    assert "no_such_program" in result["stdout_tail"]


# A replay driver written in Python rather than Fortran, so the property
# stage can be exercised where no compiler is installed. It does what the
# fixture region does: one step, adding one to what it was given.
REPLAY_SCRIPT = """#!/bin/sh
exec "PYTHON" - "$1" <<'PYEOF'
import sys
from pathlib import Path
import numpy as np
case = Path(sys.argv[1])
np.save(case / "h.out.npy", np.load(case / "h.npy") + 1)
PYEOF
"""

# One property this replay has and one it does not. The failing one is
# wrong on purpose: what the stage has to report is which of the two
# failed and how many ran, not that everything was fine.
PROPERTIES_MODULE = '''"""Invariants of a code, run against its own replay binary."""
from hypothesis import given, strategies as st

import harness_properties as harness


@harness.settings()
@given(st.integers(min_value=0, max_value=3))
def test_the_same_inputs_replay_to_the_same_outputs(offset):
    inputs = {"h": harness.corpus()[0]["h"] + offset}
    first = harness.run_replay(inputs)["h"]
    second = harness.run_replay(inputs)["h"]
    assert (first == second).all()


def test_the_replay_hands_back_what_it_was_given():
    inputs = harness.corpus()[0]
    assert (harness.run_replay(inputs)["h"] == inputs["h"]).all()


def test_the_seed_reached_the_module():
    assert harness.seed() == 4242
'''

def _npy(values) -> bytes:
    """One array as the bytes of an NPY file, which is how a case travels."""
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(values, dtype="<f4"), allow_pickle=False)
    return buffer.getvalue()


PROPERTY_CASES = {"case0000": {"h": base64.b64encode(_npy([1.0, 2.0, 3.0])).decode()}}


def _property_tree(tmp_path, files=None):
    """A workspace holding a replay binary and whatever else the test wants."""
    import os
    import sys

    written = {"replay": REPLAY_SCRIPT.replace("PYTHON", sys.executable)}
    written.update(files or {"harness/properties.py": PROPERTIES_MODULE})
    tree_dir = stages.write_tree(tmp_path / "attempt-1" / "tree", tree_of(written))
    os.chmod(Path(tree_dir) / "replay", 0o755)
    return tree_dir


needs_hypothesis = pytest.mark.skipif(
    importlib.util.find_spec("hypothesis") is None,
    reason="running a code's property module needs Hypothesis",
)


@needs_hypothesis
def test_properties_runs_the_module_and_reports_what_passed_and_what_failed(tmp_path):
    _property_tree(tmp_path)

    result = stages.properties(
        "attempt-1", "replay", "harness/properties.py", PROPERTY_CASES,
        seed=4242, max_examples=5, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is False  # one of the three properties does not hold
    assert result["passed"] == 2
    assert result["failed"] == 1
    assert result["errors"] == 0
    assert result["seed"] == 4242
    assert result["max_examples"] == 5
    assert "test_the_replay_hands_back_what_it_was_given" in result["log_tail"]


@needs_hypothesis
def test_properties_passes_when_every_property_holds(tmp_path):
    _property_tree(tmp_path, files={"harness/properties.py": (
        "import harness_properties as harness\n"
        "\n"
        "\n"
        "def test_one_step_adds_one():\n"
        "    inputs = harness.corpus()[0]\n"
        "    assert (harness.run_replay(inputs)['h'] == inputs['h'] + 1).all()\n"
    )})

    result = stages.properties(
        "attempt-1", "replay", "harness/properties.py", PROPERTY_CASES,
        seed=7, max_examples=5, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is True
    assert result["passed"] == 1
    assert result["failed"] == 0


@needs_hypothesis
def test_the_cases_reach_the_module_as_the_corpus_it_reads(tmp_path):
    # The property module never sees the wire format: it asks for the
    # corpus and gets arrays, which is the whole point of the library.
    _property_tree(tmp_path, files={"harness/properties.py": (
        "import harness_properties as harness\n"
        "\n"
        "\n"
        "def test_the_corpus_is_the_visible_cases():\n"
        "    corpus = harness.corpus()\n"
        "    assert len(corpus) == 1\n"
        "    assert list(corpus[0]['h']) == [1.0, 2.0, 3.0]\n"
    )})

    result = stages.properties(
        "attempt-1", "replay", "harness/properties.py", PROPERTY_CASES,
        seed=7, max_examples=5, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is True, result["log_tail"]


@pytest.mark.parametrize("module", ["../elsewhere/properties.py", "/etc/properties.py"])
def test_a_properties_module_outside_the_tree_is_refused_by_name(tmp_path, module):
    _property_tree(tmp_path)

    result = stages.properties(
        "attempt-1", "replay", module, PROPERTY_CASES,
        seed=1, max_examples=5, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is False
    assert module in result["log_tail"]


def test_a_properties_module_the_tree_does_not_hold_is_refused_by_name(tmp_path):
    _property_tree(tmp_path)

    result = stages.properties(
        "attempt-1", "replay", "harness/nothing_here.py", PROPERTY_CASES,
        seed=1, max_examples=5, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is False
    assert "harness/nothing_here.py" in result["log_tail"]


def test_properties_reports_the_replay_executable_when_it_is_missing(tmp_path):
    _property_tree(tmp_path)

    result = stages.properties(
        "attempt-1", "some_other_binary", "harness/properties.py", PROPERTY_CASES,
        seed=1, max_examples=5, work_root=tmp_path, harness_dir=HARNESS,
    )

    assert result["ok"] is False
    assert "some_other_binary" in result["log_tail"]
