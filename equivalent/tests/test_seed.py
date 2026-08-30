"""Writing the baseline seed, the way the deployment writes it before first start.

The module under test lives in `deploy/`, which is deployment rather than
package code; the test lives here so that running the suite from the
repository root covers it along with everything else.
"""
from pathlib import Path

from deploy.seed import baseline_dir, baseline_paths, write_seed
from equivalent.ledger.subjects import tree_subject

REPO_ROOT = Path(__file__).resolve().parents[2]
CODE = "tsunami"

# The baseline is these nine files and nothing else. Naming them here is
# the point of the test: the directory they are read from also holds
# compiler output, and a seed that grew a .mod file or a binary would
# change the baseline hash without anyone choosing to.
#
# The ones under harness/ are the replay driver, the capture program, the
# property module, and the tolerance policy. They are the code's own, built or read by the
# code's own makefile and frozen while a region of it is ported -- no
# strategy allows them to be edited.
BASELINE = [
    "Makefile",
    "harness/gen_reference.f90",
    "harness/properties.py",
    "harness/replay.f90",
    "harness/tolerances.json",
    "src/mod_diff.f90",
    "src/mod_initial.f90",
    "src/mod_kernel.f90",
    "src/mod_params.f90",
    "src/tsunami.f90",
]


def _seeded_files(directory: Path) -> list[dict]:
    return [
        {"path": str(p.relative_to(directory)), "content": p.read_bytes()}
        for p in sorted(directory.rglob("*")) if p.is_file()
    ]


def test_the_baseline_directory_comes_from_the_codes_manifest():
    # Not from a constant in the script: a second code is onboarded by
    # writing its manifest, not by editing the deployment.
    assert baseline_dir(REPO_ROOT, CODE) == f"programs/{CODE}/baseline"


def test_the_seed_is_exactly_the_tracked_baseline_files(tmp_path):
    written = write_seed(REPO_ROOT, tmp_path, CODE)

    assert written == BASELINE
    assert sorted(f["path"] for f in _seeded_files(tmp_path)) == BASELINE


def test_the_paths_it_reports_are_the_paths_it_writes(tmp_path):
    assert baseline_paths(REPO_ROOT, CODE) == write_seed(REPO_ROOT, tmp_path, CODE)


def test_two_seedings_produce_the_same_baseline_hash(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_seed(REPO_ROOT, first, CODE)
    write_seed(REPO_ROOT, second, CODE)

    assert tree_subject(_seeded_files(first)) == tree_subject(_seeded_files(second))
