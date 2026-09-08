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
import pytest

from equivalent.components import manifest_check
from equivalent.manifest.schema import IN_TREE_MANIFEST
from equivalent.tests.fakes import (
    FIXTURE_VARIABLES,
    PROGRAM_TOLERANCES,
    TOLERANCES_IN_TREE,
    in_tree_manifest,
    write_tree,
)


def _check(harness, manifest: dict | None = None, tolerances: dict | None = None):
    """The manifest check on a tree that says this about itself."""
    seed = write_tree(harness.tmp_path / "seed", manifest)
    if tolerances is not None:
        (seed / TOLERANCES_IN_TREE).write_text(json.dumps(tolerances))
    harness.repo(seed)
    return manifest_check.check(harness.context(), {})


def test_a_well_formed_manifest_passes_and_says_what_the_code_is(harness):
    result = _check(harness)

    assert result.verdict == "pass"
    detail = result.detail
    assert detail["name"] == "tsunami"
    assert len(detail["manifest_sha256"]) == 64
    # What every later claim about this tree was filed under, in the words
    # the manifest used: the targets, the region's variables, the datasets.
    assert sorted(detail["targets"]) == ["capture", "replay", "timing"]
    assert detail["outputs"] == [v["name"] for v in FIXTURE_VARIABLES]
    assert detail["datasets"] == ["holdout", "visible"]


def test_a_tree_with_no_manifest_in_it_fails_and_names_the_path(harness):
    seed = write_tree(harness.tmp_path / "seed")
    (seed / IN_TREE_MANIFEST).unlink()
    harness.repo(seed)

    result = manifest_check.check(harness.context(), {})

    assert result.verdict == "fail"
    assert IN_TREE_MANIFEST in result.detail["reason"]


def test_a_manifest_still_in_its_minimal_form_fails_and_names_what_is_absent(harness):
    minimal = {key: in_tree_manifest()[key] for key in ("version", "name", "source")}

    result = _check(harness, minimal)

    assert result.verdict == "fail"
    assert "interface" in result.detail["reason"]
    assert "datasets" in result.detail["reason"]


def test_a_manifest_naming_a_file_the_tree_does_not_hold_fails_in_the_trees_own_words(harness):
    manifest = copy.deepcopy(in_tree_manifest())
    manifest["build"]["makefile"] = "build/Makefile.nowhere"

    result = _check(harness, manifest)

    assert result.verdict == "fail"
    reason = result.detail["reason"]
    assert "Makefile.nowhere" in reason
    # The path is spelled the way the agent's own tree spells it; where
    # the gateway unpacked the tree is not the agent's business.
    assert str(harness.tmp_path) not in reason


def test_a_floating_point_output_with_no_tolerance_band_fails_and_names_it(harness):
    unbanded = FIXTURE_VARIABLES[1]["name"]
    thinned = {
        **PROGRAM_TOLERANCES,
        "variables": {
            name: band for name, band in PROGRAM_TOLERANCES["variables"].items()
            if name != unbanded
        },
    }

    result = _check(harness, tolerances=thinned)

    assert result.verdict == "fail"
    assert any(unbanded in problem for problem in result.detail["problems"])


def test_a_tolerance_entry_missing_one_of_its_three_numbers_fails(harness):
    name = FIXTURE_VARIABLES[0]["name"]
    partial = {
        **PROGRAM_TOLERANCES,
        "variables": {**PROGRAM_TOLERANCES["variables"], name: {"abs": 1e-6}},
    }

    result = _check(harness, tolerances=partial)

    assert result.verdict == "fail"
    problems = " ".join(result.detail["problems"])
    assert name in problems and "rel" in problems and "ulp" in problems


@pytest.mark.parametrize(("field", "value"), [
    ("abs", float("nan")), ("rel", float("inf")), ("abs", -1.0),
    ("ulp", -1), ("ulp", 1.5), ("ulp", True),
])
def test_an_invalid_tolerance_number_fails_manifest_validation(harness, field, value):
    name = FIXTURE_VARIABLES[0]["name"]
    invalid = copy.deepcopy(PROGRAM_TOLERANCES)
    invalid["variables"][name][field] = value

    result = _check(harness, tolerances=invalid)

    assert result.verdict == "fail"
    assert any(name in problem and field in problem for problem in result.detail["problems"])


def test_a_tolerance_file_that_is_not_a_policy_at_all_fails(harness):
    result = _check(harness, tolerances={"policy_version": "1"})

    assert result.verdict == "fail"
    assert "variables" in " ".join(result.detail["problems"])


def test_a_timing_output_that_is_not_an_npy_file_fails_and_names_it(harness):
    manifest = copy.deepcopy(in_tree_manifest())
    manifest["timing"]["outputs"] = ["run.log"]

    result = _check(harness, manifest)

    assert result.verdict == "fail"
    assert any("run.log" in problem for problem in result.detail["problems"])


def test_a_timing_output_with_no_tolerance_band_fails_and_names_it(harness):
    # Nothing declares what a file the timing run writes holds, so every
    # one of them is compared within a band or not at all.
    tolerances = copy.deepcopy(PROGRAM_TOLERANCES)
    del tolerances["files"]["results/flux.npy"]

    result = _check(harness, tolerances=tolerances)

    assert result.verdict == "fail"
    assert any("results/flux.npy" in problem for problem in result.detail["problems"])


def test_a_manifest_that_is_not_yaml_at_all_is_a_verdict_and_not_an_error(harness):
    # The file is the agent's submission, so its being wrong is an answer
    # about the code, not a failure of the harness.
    seed = write_tree(harness.tmp_path / "seed")
    (seed / IN_TREE_MANIFEST).write_text("version: 1\n  name: [unclosed\n")
    harness.repo(seed)

    result = manifest_check.check(harness.context(), {})

    assert result.verdict == "fail"
    assert result.detail["reason"]


def test_the_visible_and_held_out_runs_have_to_differ(harness):
    manifest = copy.deepcopy(in_tree_manifest())
    manifest["datasets"]["holdout"] = dict(manifest["datasets"]["visible"])

    result = _check(harness, manifest)

    assert result.verdict == "fail"
    assert "holdout" in result.detail["reason"]


def test_the_manifest_it_read_is_the_one_the_tree_holds(harness):
    result = _check(harness)
    written = (harness.tmp_path / "seed" / IN_TREE_MANIFEST).read_bytes()

    from equivalent.ledger.subjects import hash_bytes
    assert result.detail["manifest_sha256"] == hash_bytes(written)
    # And the file really is the fixture's, not something this test built
    # a second time.
    assert yaml.safe_load(written)["name"] == "tsunami"
