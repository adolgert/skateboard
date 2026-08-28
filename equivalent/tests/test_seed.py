"""Writing the baseline seed, the way the deployment writes it before first start.

The module under test lives in `deploy/`, which is deployment rather than
package code; the test lives here so that running the suite from the
repository root covers it along with everything else.
"""
from pathlib import Path

from deploy.seed import baseline_paths, write_seed
from equivalent.ledger.subjects import tree_subject

REPO_ROOT = Path(__file__).resolve().parents[2]

# The baseline is these six files and nothing else. Naming them here is
# the point of the test: the directory they are read from also holds
# compiler output, and a seed that grew a .mod file or a binary would
# change the baseline hash without anyone choosing to.
BASELINE = [
    "Makefile",
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


def test_the_seed_is_exactly_the_tracked_baseline_files(tmp_path):
    written = write_seed(REPO_ROOT, tmp_path)

    assert written == BASELINE
    assert sorted(f["path"] for f in _seeded_files(tmp_path)) == BASELINE


def test_the_paths_it_reports_are_the_paths_it_writes(tmp_path):
    assert baseline_paths(REPO_ROOT) == write_seed(REPO_ROOT, tmp_path)


def test_two_seedings_produce_the_same_baseline_hash(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_seed(REPO_ROOT, first)
    write_seed(REPO_ROOT, second)

    assert tree_subject(_seeded_files(first)) == tree_subject(_seeded_files(second))
