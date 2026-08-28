"""Reading the compiler log the shim writes, which is the whole build proof."""
import json

import pytest

from equivalent.manifest.schema import _matches as manifest_matches
from services.builder import contract

FLAGS = ["-O2", "-stdpar=gpu"]
PATTERNS = ["src/*.f90", "harness/*.f90"]


def _log(*entries) -> str:
    return "".join(json.dumps(entry) + "\n" for entry in entries)


def _tree(tmp_path, *relative_paths):
    """A tree directory holding the named files, and its path."""
    tree = tmp_path / "tree"
    for relative in relative_paths:
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("! fortran\n")
    return tree


def test_one_compile_reports_its_inputs_output_and_flags(tmp_path):
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    log = _log({
        "argv": ["-O2", "-stdpar=gpu", "-module", ".", "-o", "replay", "src/mod_kernel.f90"],
        "cwd": str(tree),
    })

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert len(records) == 1
    assert records[0]["inputs"] == ["src/mod_kernel.f90"]
    assert records[0]["output"] == "replay"
    assert records[0]["has_flags"] is True
    assert records[0]["outside"] == []


def test_a_compile_missing_one_strategy_flag_is_marked(tmp_path):
    # The Makefile that hard-codes its own FFLAGS: the compile happens, it
    # succeeds, and only the log says the strategy never reached it.
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    log = _log({"argv": ["-O0", "-o", "replay", "src/mod_kernel.f90"], "cwd": str(tree)})

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert records[0]["has_flags"] is False
    assert contract.flags_reached_every_compile(records) is False
    assert contract.compiles_without_flags(records) == [records[0]["argv"]]


def test_a_source_file_from_outside_the_tree_is_named(tmp_path):
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    elsewhere = tmp_path / "elsewhere" / "sneak.f90"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("! not the tree's\n")
    log = _log({
        "argv": [*FLAGS, "-o", "replay", "src/mod_kernel.f90", str(elsewhere)],
        "cwd": str(tree),
    })

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert records[0]["outside"] == [str(elsewhere)]
    assert contract.compiled_only_tree_source(records) is False
    assert contract.files_outside_tree(records) == [str(elsewhere)]


def test_a_file_under_the_harness_directory_is_not_outside(tmp_path):
    # The builder's own NPY reader is not the tree's file and never
    # matches the code's source patterns, but the tree is meant to
    # compile it: it is what the capture format is written with.
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    harness = tmp_path / "harness_dir"
    harness.mkdir()
    (harness / "npy_io.f90").write_text("! baked\n")
    log = _log({
        "argv": [*FLAGS, "-o", "replay", str(harness / "npy_io.f90"), "src/mod_kernel.f90"],
        "cwd": str(tree),
    })

    records = contract.compile_records(log, tree, FLAGS, PATTERNS, harness_dir=harness)

    assert records[0]["outside"] == []
    assert contract.compiled_only_tree_source(records) is True


def test_a_tree_file_the_patterns_do_not_cover_is_outside(tmp_path):
    # Inside the tree is not enough: the code says which of its files are
    # source, and a build reaching past that list is building something
    # the manifest never described.
    tree = _tree(tmp_path, "src/mod_kernel.f90", "scratch/experiment.f90")
    log = _log({
        "argv": [*FLAGS, "-o", "replay", "src/mod_kernel.f90", "scratch/experiment.f90"],
        "cwd": str(tree),
    })

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert records[0]["outside"] == ["scratch/experiment.f90"]


def test_two_sources_with_the_same_basename_are_both_recorded(tmp_path):
    tree = _tree(tmp_path, "src/a/x.f90", "src/b/x.f90")
    log = _log({
        "argv": [*FLAGS, "-o", "prog", "src/a/x.f90", "src/b/x.f90"],
        "cwd": str(tree),
    })

    records = contract.compile_records(log, tree, FLAGS, ["src/*.f90"])

    assert records[0]["inputs"] == ["src/a/x.f90", "src/b/x.f90"]


def test_a_path_is_resolved_against_the_directory_the_compile_ran_in(tmp_path):
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    log = _log({
        "argv": [*FLAGS, "-c", "mod_kernel.f90"], "cwd": str(tree / "src"),
    })

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert records[0]["inputs"] == ["src/mod_kernel.f90"]
    assert records[0]["cwd"] == "src"


def test_an_argument_that_is_not_a_file_on_disk_is_not_an_input(tmp_path):
    # "-o replay.f90" would name a Fortran extension without being one of
    # the tree's files; only something that is really there counts.
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    log = _log({
        "argv": [*FLAGS, "-o", "missing.f90", "src/mod_kernel.f90"], "cwd": str(tree),
    })

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert records[0]["inputs"] == ["src/mod_kernel.f90"]
    assert records[0]["output"] == "missing.f90"


def test_a_link_step_with_no_source_does_not_decide_the_flag_question(tmp_path):
    # A separate link line carries LDFLAGS, not FFLAGS. It is reported so
    # a reader sees it, but "did the flags reach every compile" is a
    # question about compiles.
    tree = _tree(tmp_path, "src/mod_kernel.f90")
    log = _log(
        {"argv": [*FLAGS, "-c", "src/mod_kernel.f90"], "cwd": str(tree)},
        {"argv": ["-o", "replay", "mod_kernel.o"], "cwd": str(tree)},
    )

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert len(records) == 2
    assert records[1]["inputs"] == []
    assert contract.flags_reached_every_compile(records) is True


def test_a_log_with_no_compile_at_all_reaches_no_flags(tmp_path):
    # An empty log means the Makefile never called the compiler the
    # builder handed it -- a build that proved nothing, not a clean one.
    tree = _tree(tmp_path, "src/mod_kernel.f90")

    records = contract.compile_records("\n \n", tree, FLAGS, PATTERNS)

    assert records == []
    assert contract.flags_reached_every_compile(records) is False


def test_an_uppercase_extension_is_still_fortran_source(tmp_path):
    tree = _tree(tmp_path, "src/MOD_KERNEL.F90")
    log = _log({"argv": [*FLAGS, "-c", "src/MOD_KERNEL.F90"], "cwd": str(tree)})

    records = contract.compile_records(log, tree, FLAGS, PATTERNS)

    assert records[0]["inputs"] == ["src/MOD_KERNEL.F90"]
    assert records[0]["outside"] == []


def test_a_malformed_log_line_is_reported_rather_than_ignored(tmp_path):
    tree = _tree(tmp_path, "src/mod_kernel.f90")

    with pytest.raises(ValueError):
        contract.compile_records("this is not json\n", tree, FLAGS, PATTERNS)


@pytest.mark.parametrize(
    "compiler,expected",
    [
        ("nvfortran", "-module"),
        ("/opt/nvidia/hpc_sdk/bin/nvfortran", "-module"),
        ("gfortran", "-J"),
        ("gfortran-13", None),
        ("some-other-compiler", None),
    ],
)
def test_the_module_directory_flag_is_looked_up_by_compiler_name(compiler, expected):
    assert contract.module_flag(compiler) == expected


# The same paths and patterns put to both copies of the source-pattern
# rule. The builder image does not install the gateway's package, so
# contract.py carries its own copy; this is what keeps the two the same
# rule rather than two rules that happen to agree today.
PATTERN_TABLE = [
    ("src/mod_kernel.f90", "src/*.f90"),
    ("src/deep/mod_kernel.f90", "src/*.f90"),
    ("src/PESs/N4_UMN_PES_Class.F90", "src/*.f90"),
    ("mod_kernel.f90", "**/*.f90"),
    ("src/mod_kernel.f90", "**/*.f90"),
    ("./src/mod_kernel.f90", "**/*.f90"),
    ("Makefile", "Makefile"),
    ("Makefile", "**/*.f90"),
    ("MAKEFILE", "Makefile"),
    ("src/mod_kernel.c", "src/*.f90"),
    ("harness/replay.f90", "src/*.f90"),
    ("src\\mod_kernel.f90", "src/*.f90"),
]


@pytest.mark.parametrize("path,pattern", PATTERN_TABLE)
def test_the_builders_copy_of_the_pattern_rule_agrees_with_the_manifests(path, pattern):
    assert contract._matches(path, pattern) == manifest_matches(path, pattern)
