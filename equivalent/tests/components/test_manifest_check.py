"""Judging the manifest a submitted tree carries.

These read as the statement of what a code has to say about itself
before anything else is measured: a manifest that is there, complete,
consistent with the files beside it, and specific about how every
floating-point output will be compared.
"""
from __future__ import annotations

import copy
import json

import yaml

from equivalent.components import manifest_check
from equivalent.gateway.submit import init_baseline_repo
from equivalent.manifest.schema import IN_TREE_MANIFEST
from equivalent.tests.fakes import (
    FIXTURE_VARIABLES,
    PROGRAM_TOLERANCES,
    TOLERANCES_IN_TREE,
    in_tree_manifest,
    write_tree,
)


def _repo(tmp_path, manifest: dict | None = None, tolerances: dict | None = None):
    """A gateway repository whose baseline is one onboarding tree."""
    seed = write_tree(tmp_path / "seed", manifest)
    if tolerances is not None:
        (seed / TOLERANCES_IN_TREE).write_text(json.dumps(tolerances))
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)
    return repo


def test_a_well_formed_manifest_passes_and_says_what_the_code_is(tmp_path):
    result = manifest_check.check(_repo(tmp_path), "main")

    assert result["verdict"] == "pass"
    detail = result["detail"]
    assert detail["name"] == "tsunami"
    assert len(detail["manifest_sha256"]) == 64
    # What every later claim about this tree was filed under, in the words
    # the manifest used: the targets, the region's variables, the datasets.
    assert sorted(detail["targets"]) == ["capture", "replay", "timing"]
    assert detail["outputs"] == [v["name"] for v in FIXTURE_VARIABLES]
    assert detail["datasets"] == ["holdout", "visible"]


def test_a_tree_with_no_manifest_in_it_fails_and_names_the_path(tmp_path):
    seed = write_tree(tmp_path / "seed")
    (seed / IN_TREE_MANIFEST).unlink()
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)

    result = manifest_check.check(repo, "main")

    assert result["verdict"] == "fail"
    assert IN_TREE_MANIFEST in result["detail"]["reason"]


def test_a_manifest_still_in_its_minimal_form_fails_and_names_what_is_absent(tmp_path):
    minimal = {key: in_tree_manifest()[key] for key in ("version", "name", "source")}

    result = manifest_check.check(_repo(tmp_path, minimal), "main")

    assert result["verdict"] == "fail"
    assert "interface" in result["detail"]["reason"]
    assert "datasets" in result["detail"]["reason"]


def test_a_manifest_naming_a_file_the_tree_does_not_hold_fails_in_the_trees_own_words(tmp_path):
    manifest = copy.deepcopy(in_tree_manifest())
    manifest["build"]["makefile"] = "build/Makefile.nowhere"

    result = manifest_check.check(_repo(tmp_path, manifest), "main")

    assert result["verdict"] == "fail"
    reason = result["detail"]["reason"]
    assert "Makefile.nowhere" in reason
    # The path is spelled the way the agent's own tree spells it; where
    # the gateway unpacked the tree is not the agent's business.
    assert str(tmp_path) not in reason


def test_a_floating_point_output_with_no_tolerance_band_fails_and_names_it(tmp_path):
    unbanded = FIXTURE_VARIABLES[1]["name"]
    thinned = {
        **PROGRAM_TOLERANCES,
        "variables": {
            name: band for name, band in PROGRAM_TOLERANCES["variables"].items()
            if name != unbanded
        },
    }

    result = manifest_check.check(_repo(tmp_path, tolerances=thinned), "main")

    assert result["verdict"] == "fail"
    assert any(unbanded in problem for problem in result["detail"]["problems"])


def test_a_tolerance_entry_missing_one_of_its_three_numbers_fails(tmp_path):
    name = FIXTURE_VARIABLES[0]["name"]
    partial = {
        **PROGRAM_TOLERANCES,
        "variables": {**PROGRAM_TOLERANCES["variables"], name: {"abs": 1e-6}},
    }

    result = manifest_check.check(_repo(tmp_path, tolerances=partial), "main")

    assert result["verdict"] == "fail"
    problems = " ".join(result["detail"]["problems"])
    assert name in problems and "rel" in problems and "ulp" in problems


def test_a_tolerance_file_that_is_not_a_policy_at_all_fails(tmp_path):
    result = manifest_check.check(_repo(tmp_path, tolerances={"policy_version": "1"}), "main")

    assert result["verdict"] == "fail"
    assert "variables" in " ".join(result["detail"]["problems"])


def test_a_timing_output_that_is_not_an_npy_file_fails_and_names_it(tmp_path):
    manifest = copy.deepcopy(in_tree_manifest())
    manifest["timing"]["outputs"] = ["run.log"]

    result = manifest_check.check(_repo(tmp_path, manifest), "main")

    assert result["verdict"] == "fail"
    assert any("run.log" in problem for problem in result["detail"]["problems"])


def test_a_manifest_that_is_not_yaml_at_all_is_a_verdict_and_not_an_error(tmp_path):
    # The file is the agent's submission, so its being wrong is an answer
    # about the code, not a failure of the harness.
    seed = write_tree(tmp_path / "seed")
    (seed / IN_TREE_MANIFEST).write_text("version: 1\n  name: [unclosed\n")
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)

    result = manifest_check.check(repo, "main")

    assert result["verdict"] == "fail"
    assert result["detail"]["reason"]


def test_the_visible_and_held_out_runs_have_to_differ(tmp_path):
    manifest = copy.deepcopy(in_tree_manifest())
    manifest["datasets"]["holdout"] = dict(manifest["datasets"]["visible"])

    result = manifest_check.check(_repo(tmp_path, manifest), "main")

    assert result["verdict"] == "fail"
    assert "holdout" in result["detail"]["reason"]


def test_the_manifest_it_read_is_the_one_the_tree_holds(tmp_path):
    repo = _repo(tmp_path)
    written = (tmp_path / "seed" / IN_TREE_MANIFEST).read_bytes()

    result = manifest_check.check(repo, "main")

    from equivalent.ledger.subjects import hash_bytes
    assert result["detail"]["manifest_sha256"] == hash_bytes(written)
    # And the file really is the fixture's, not something this test built
    # a second time.
    assert yaml.safe_load(written)["name"] == "tsunami"
