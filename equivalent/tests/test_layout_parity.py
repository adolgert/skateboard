"""The copies of a code directory's layout that live outside the package.

Two programs spell these names for themselves. The oracle image is
sealed: the whole code directory is copied into it and the package is
not, so it cannot import the one spelling and has to repeat it. The seed
script runs on the host with whatever `python3` is on the path, before
anything is installed, so it stays free of the package for the same
practical reason.

Both copies are silent when they drift: an oracle looking for `captures`
under a code directory that now writes `answers` reports itself
not-ready rather than wrong, and a seed script looking for the wrong
manifest name writes no baseline at all. This is the test that notices.
"""
from deploy import seed
from equivalent.manifest.layout import CAPTURES_DIR, MANIFEST_NAME
from equivalent.manifest.schema import REQUIRED_DATASETS
from services.oracle import app as oracle


def test_the_sealed_oracle_looks_for_the_manifest_the_package_writes():
    assert oracle.MANIFEST_NAME == MANIFEST_NAME


def test_the_sealed_oracle_answers_for_the_datasets_every_manifest_declares():
    assert oracle.DATASETS == REQUIRED_DATASETS


def test_the_sealed_oracle_reads_the_captures_directory_promoting_writes():
    assert oracle.CAPTURES_NAME == CAPTURES_DIR


def test_the_seed_script_looks_for_the_manifest_the_package_writes():
    assert seed.MANIFEST_NAME == MANIFEST_NAME
