"""Reading a code's manifest, the way a deployment writes one per code."""
import copy
from pathlib import Path

import pytest
import yaml

from equivalent.manifest.schema import (
    COMPLETING_FIELDS,
    IN_TREE_MANIFEST,
    load_manifest,
    load_tree_manifest,
    source_files,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TSUNAMI = REPO_ROOT / "programs" / "tsunami" / "manifest.yaml"

MANIFEST = {
    "version": 1,
    "name": "tsunami",
    "source": {"root": "baseline", "patterns": ["**/*.f90", "Makefile"]},
    "build": {
        "makefile": "Makefile",
        "targets": {
            "replay": {"target": "replay", "executable": "replay"},
            "timing": {"target": "timing", "executable": "tsunami"},
        },
    },
    "interface": {
        "module": "mod_kernel",
        "entry": "step",
        "inputs": [{"name": "field", "dtype": "f32", "rank": 1}],
        "outputs": [{"name": "field", "dtype": "f32", "rank": 1}],
    },
    "datasets": {
        "visible": {"args": ["100", "5000", "25", "0.02"]},
        "holdout": {"args": ["100", "5000", "60", "0.01"]},
    },
    "timing": {"args": [], "outputs": [], "budget_s": 300},
    "tolerances": "tolerances.json",
    "properties": None,
}


MINIMAL = {key: MANIFEST[key] for key in ("version", "name", "source")}


def _write(tmp_path, manifest: dict) -> Path:
    """A code directory laid out the way `programs/<name>/` is, plus its manifest.

    Everything the manifest names other than the source root lives inside
    the tree, which is where the loader looks for it.
    """
    tree = tmp_path / "baseline"
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "mod_kernel.f90").write_text("end\n")
    (tree / "Makefile").write_text("replay:\n\techo build\n")
    (tree / "tolerances.json").write_text("{}\n")
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def _write_in_tree(tree, manifest: dict) -> Path:
    """The form a manifest takes while it is being written: inside the tree."""
    (tree / "src").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "mod_kernel.f90").write_text("end\n")
    (tree / "Makefile").write_text("replay:\n\techo build\n")
    path = tree / IN_TREE_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    (tree / "harness" / "tolerances.json").write_text("{}\n")
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def test_the_tsunami_manifest_loads_and_resolves_its_paths():
    manifest = load_manifest(TSUNAMI)

    assert manifest.name == "tsunami"
    assert manifest.source.root == TSUNAMI.parent / "baseline"
    assert manifest.source.root.is_dir()
    # Every path but the source root is read from inside the tree, so the
    # same manifest text works beside the tree and inside it.
    assert manifest.tolerances == TSUNAMI.parent / "baseline" / "harness" / "tolerances.json"
    assert manifest.complete
    assert manifest.build.targets["replay"].target == "replay"
    assert manifest.build.targets["timing"].executable == "tsunami"
    # The region reads and writes the same two arrays, which is what makes
    # a one-step replay of it possible at all.
    assert len(manifest.interface.outputs) == 2
    assert [v.name for v in manifest.interface.outputs] == [
        v.name for v in manifest.interface.inputs
    ]
    assert manifest.datasets["visible"].args == ("100", "5000", "25", "0.02")
    assert manifest.timing.budget_s == 300
    # The code carries its own invariants, and the path resolves inside
    # the tree the same way the tolerance policy's does.
    assert manifest.properties == TSUNAMI.parent / "baseline" / "harness" / "properties.py"


def test_a_loaded_manifest_names_itself_as_a_subject(tmp_path):
    manifest = load_manifest(_write(tmp_path, MANIFEST))

    subject = manifest.as_subject()

    assert subject.kind == "manifest"
    assert subject.sha256 == manifest.sha256


@pytest.mark.parametrize("field", sorted(MANIFEST))
def test_every_required_field_is_named_when_it_is_missing(tmp_path, field):
    raw = copy.deepcopy(MANIFEST)
    del raw[field]

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert field in str(excinfo.value)


def test_an_unknown_key_names_the_key(tmp_path):
    raw = {**copy.deepcopy(MANIFEST), "toolerances": "tolerances.json"}

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "toolerances" in str(excinfo.value)


def test_an_unknown_key_inside_a_section_names_the_key(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["source"]["pattern"] = ["*.f90"]

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "pattern" in str(excinfo.value)


def test_a_type_the_harness_cannot_carry_is_rejected_by_name(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["interface"]["inputs"][0]["dtype"] = "complex64"

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "complex64" in str(excinfo.value)


def test_a_rank_beyond_what_the_harness_carries_is_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["interface"]["outputs"][0]["rank"] = 5

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "rank" in str(excinfo.value)


def test_an_empty_name_is_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["interface"]["entry"] = ""

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "entry" in str(excinfo.value)


def test_a_build_with_no_replay_target_is_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    del raw["build"]["targets"]["replay"]

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "replay" in str(excinfo.value)


def test_a_missing_holdout_dataset_is_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    del raw["datasets"]["holdout"]

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "holdout" in str(excinfo.value)


def test_a_holdout_run_the_same_as_the_visible_one_is_rejected(tmp_path):
    # A held-out set generated from the visible set's own parameters is
    # not held out at all: the agent has already seen those answers.
    raw = copy.deepcopy(MANIFEST)
    raw["datasets"]["holdout"] = {"args": list(raw["datasets"]["visible"]["args"])}

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "holdout" in str(excinfo.value)


def test_a_source_root_that_is_not_a_directory_is_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["source"]["root"] = "nowhere"

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "nowhere" in str(excinfo.value)


def test_a_tolerances_path_that_is_not_a_file_is_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["tolerances"] = "no-such-policy.json"

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "no-such-policy.json" in str(excinfo.value)


def test_the_hash_changes_when_a_byte_of_the_file_changes(tmp_path):
    first = load_manifest(_write(tmp_path, MANIFEST))
    changed = copy.deepcopy(MANIFEST)
    changed["timing"]["budget_s"] = 301
    second = load_manifest(_write(tmp_path, changed))

    assert first.sha256 != second.sha256


def test_source_files_keeps_the_paths_that_count_as_source(tmp_path):
    manifest = load_manifest(TSUNAMI)

    kept = source_files(manifest, [
        "src/a.F90", "Makefile", "x/y/z.f", "README.md",
    ])

    assert kept == ["src/a.F90", "Makefile", "x/y/z.f"]


def test_a_timing_run_declares_no_environment_by_default(tmp_path):
    manifest = load_manifest(_write(tmp_path, MANIFEST))

    assert manifest.timing.env == {}


def test_the_timing_environment_is_carried_as_written(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["timing"]["env"] = {"MALLOC_TRIM_THRESHOLD_": "-1"}

    manifest = load_manifest(_write(tmp_path, raw))

    assert manifest.timing.env == {"MALLOC_TRIM_THRESHOLD_": "-1"}


def test_a_timing_environment_value_that_is_not_a_string_is_rejected_by_name(tmp_path):
    # A bare -1 in YAML is an integer, and what reaches the program has to
    # be exactly the text the file shows.
    raw = copy.deepcopy(MANIFEST)
    raw["timing"]["env"] = {"MALLOC_TRIM_THRESHOLD_": -1}

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "MALLOC_TRIM_THRESHOLD_" in str(excinfo.value)


def test_an_unknown_timing_key_is_still_rejected(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["timing"]["environment"] = {}

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "environment" in str(excinfo.value)


def test_the_tsunami_timing_run_keeps_the_allocator_arena():
    # The pristine kernel returns difference arrays by value, so the CPU
    # baseline allocates temporaries every step; keeping the arena is what
    # makes the timing measure the stencil rather than page faults.
    manifest = load_manifest(TSUNAMI)

    assert manifest.timing.env == {
        "MALLOC_TRIM_THRESHOLD_": "-1",
        "MALLOC_MMAP_THRESHOLD_": "1073741824",
    }


def test_a_minimal_manifest_loads_and_says_it_is_not_complete(tmp_path):
    # The form a code arrives in: a tree and a name, and none of what
    # onboarding produces.
    manifest = load_manifest(_write(tmp_path, MINIMAL))

    assert manifest.name == "tsunami"
    assert manifest.source.root == tmp_path / "baseline"
    assert manifest.complete is False
    assert manifest.build is None
    assert manifest.interface is None
    assert manifest.datasets is None
    assert manifest.timing is None
    assert manifest.tolerances is None
    assert manifest.properties is None
    assert manifest.missing_parts() == list(COMPLETING_FIELDS)


@pytest.mark.parametrize("field", sorted(COMPLETING_FIELDS))
def test_a_manifest_with_some_but_not_all_of_the_six_names_what_is_absent(tmp_path, field):
    # Half a description is far more likely to be a half-written file than
    # a choice, so it is refused rather than read as the minimal form.
    raw = copy.deepcopy(MANIFEST)
    del raw[field]

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert field in str(excinfo.value)


def test_a_minimal_manifest_is_a_subject_like_any_other(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))

    assert manifest.as_subject().sha256 == manifest.sha256


def test_a_manifest_in_the_tree_resolves_its_paths_against_the_tree(tmp_path):
    raw = {**copy.deepcopy(MANIFEST), "source": {"root": ".", "patterns": ["**/*.f90"]}}
    raw["tolerances"] = "harness/tolerances.json"
    _write_in_tree(tmp_path, raw)

    manifest = load_tree_manifest(tmp_path)

    assert manifest.source.root == tmp_path
    assert manifest.tolerances == tmp_path / "harness" / "tolerances.json"
    assert manifest.complete


def test_a_manifest_in_the_tree_naming_a_source_root_other_than_the_tree_is_rejected(tmp_path):
    # A root that resolves to a real directory, so what is refused is the
    # root itself and not a path that happened to be missing under it.
    raw = {**copy.deepcopy(MANIFEST), "source": {"root": "harness", "patterns": ["**/*.f90"]}}
    raw["tolerances"] = "tolerances.json"
    _write_in_tree(tmp_path, raw)
    (tmp_path / "harness" / "Makefile").write_text("replay:\n\techo build\n")

    with pytest.raises(ValueError) as excinfo:
        load_tree_manifest(tmp_path)

    assert "source root" in str(excinfo.value)


def test_a_tree_with_no_manifest_in_it_says_which_path_it_looked_at(tmp_path):
    with pytest.raises(OSError) as excinfo:
        load_tree_manifest(tmp_path)

    assert IN_TREE_MANIFEST.split("/")[-1] in str(excinfo.value)


def test_a_makefile_the_tree_does_not_hold_is_rejected_by_name(tmp_path):
    raw = copy.deepcopy(MANIFEST)
    raw["build"]["makefile"] = "build/Makefile.nowhere"

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, raw))

    assert "Makefile.nowhere" in str(excinfo.value)
