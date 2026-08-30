"""Reading the manifest a submitted tree carries, on behalf of the checks that follow.

Every onboarding check after the manifest one is described by the tree's
own manifest, and each of them reads it from here.
"""
from __future__ import annotations

import pytest

from equivalent.components import tree_manifest
from equivalent.components.errors import ComponentError
from equivalent.gateway.submit import init_baseline_repo
from equivalent.manifest.schema import IN_TREE_MANIFEST
from equivalent.tests.fakes import write_tree


def _repo(tmp_path, drop_manifest: bool = False):
    seed = write_tree(tmp_path / "seed")
    if drop_manifest:
        (seed / IN_TREE_MANIFEST).unlink()
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)
    return repo


def test_the_manifest_the_tree_carries_is_what_comes_back(tmp_path):
    manifest = tree_manifest.manifest_of(_repo(tmp_path), "main")

    assert manifest.name == "tsunami"
    assert manifest.complete
    assert sorted(manifest.build.targets) == ["capture", "replay", "timing"]


def test_a_tree_whose_manifest_will_not_load_is_an_error_not_a_verdict(tmp_path):
    # A tree reaching one of these checks already has a passing manifest
    # claim, so a manifest that will not load now is something wrong on
    # the harness's side rather than the agent's mistake.
    with pytest.raises(ComponentError) as caught:
        tree_manifest.manifest_of(_repo(tmp_path, drop_manifest=True), "main")

    assert IN_TREE_MANIFEST in str(caught.value)
