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
import pytest

from deploy import seed
from equivalent.capture import npy
from equivalent.manifest.layout import CAPTURES_DIR, MANIFEST_NAME
from equivalent.manifest.schema import (
    COMPLETING_FIELDS, REQUIRED_DATASETS, REQUIRED_FIELDS,
    REQUIRED_INTERFACE_FIELDS, REQUIRED_SOURCE_FIELDS,
)
from services.builder import case_io
from services.builder.harness import harness_properties
from services.oracle import app as oracle


def test_the_sealed_oracle_looks_for_the_manifest_the_package_writes():
    assert oracle.MANIFEST_NAME == MANIFEST_NAME


def test_the_sealed_oracle_answers_for_the_datasets_every_manifest_declares():
    assert oracle.DATASETS == REQUIRED_DATASETS


def test_the_sealed_oracle_reads_the_captures_directory_promoting_writes():
    assert oracle.CAPTURES_NAME == CAPTURES_DIR


def test_the_seed_script_looks_for_the_manifest_the_package_writes():
    assert seed.MANIFEST_NAME == MANIFEST_NAME


def test_the_sealed_oracle_reads_the_manifest_keys_the_schema_requires():
    # It cannot import the schema, so it repeats these five key names. A
    # manifest that renamed one would leave the oracle reporting itself
    # not-ready with a complete manifest in front of it.
    assert oracle.INTERFACE_KEY in COMPLETING_FIELDS
    assert oracle.TOLERANCES_KEY in COMPLETING_FIELDS
    assert oracle.SOURCE_KEY in REQUIRED_FIELDS
    assert oracle.SOURCE_ROOT_KEY in REQUIRED_SOURCE_FIELDS
    assert oracle.OUTPUTS_KEY in REQUIRED_INTERFACE_FIELDS


# The three programs that read or write a case directory without being
# able to import the module that defines its layout: the sealed oracle,
# the builder's stages, and the property library baked into the builder
# image.
@pytest.mark.parametrize(
    "spelling", [oracle, case_io, harness_properties],
    ids=lambda module: module.__name__,
)
def test_every_copy_of_the_capture_format_spells_it_the_same_way(spelling):
    # A case directory written with one spelling and read with another is
    # a dataset that silently holds nothing.
    assert spelling.CASE_FILE == npy.CASE_FILE
    assert spelling.CASES_FILE == npy.CASES_FILE
    assert spelling.INPUT_SUFFIX == npy.INPUT_SUFFIX
    assert spelling.OUTPUT_SUFFIX == npy.OUTPUT_SUFFIX
