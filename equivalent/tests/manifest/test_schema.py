"""Reading a code's manifest, the way a deployment writes one per code."""
import copy
from pathlib import Path

import pytest
import yaml

from equivalent.manifest.schema import load_manifest, source_files

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
        "inputs": [{"name": "h", "dtype": "f32", "rank": 1}],
        "outputs": [{"name": "h", "dtype": "f32", "rank": 1}],
    },
    "datasets": {
        "visible": {"args": ["100", "5000", "25", "0.02"]},
        "holdout": {"args": ["100", "5000", "60", "0.01"]},
    },
    "timing": {"args": [], "outputs": [], "budget_s": 300},
    "tolerances": "tolerances.json",
    "properties": None,
}


def _write(tmp_path, manifest: dict) -> Path:
    """A code directory laid out the way `programs/<name>/` is, plus its manifest."""
    (tmp_path / "baseline" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "baseline" / "src" / "mod_kernel.f90").write_text("end\n")
    (tmp_path / "tolerances.json").write_text("{}\n")
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def test_the_tsunami_manifest_loads_and_resolves_its_paths():
    manifest = load_manifest(TSUNAMI)

    assert manifest.name == "tsunami"
    assert manifest.source.root == TSUNAMI.parent / "baseline"
    assert manifest.source.root.is_dir()
    assert manifest.tolerances == TSUNAMI.parent / "tolerances.json"
    assert manifest.build.targets["replay"].target == "replay"
    assert manifest.build.targets["timing"].executable == "tsunami"
    assert [v.name for v in manifest.interface.outputs] == ["h", "u"]
    assert manifest.datasets["visible"].args == ("100", "5000", "25", "0.02")
    assert manifest.timing.budget_s == 300
    assert manifest.properties is None


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
