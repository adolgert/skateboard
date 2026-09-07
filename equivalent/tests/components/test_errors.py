"""What a component does when the tree will not give up what it already gave up once.

Every check after the manifest check reads the tree's manifest to learn
what to run. That read has succeeded before, against this same tree, so
a failure now is the harness's fault and must not be recorded as a
verdict about the agent's code.
"""
import pytest

from equivalent.components.errors import ComponentError, after_the_manifest_check_passed
from equivalent.tree import init_baseline_repo
from equivalent.manifest.schema import IN_TREE_MANIFEST
from equivalent.tests.fakes import write_tree
from equivalent.tree import Tree


def test_a_tree_whose_manifest_will_not_load_is_an_error_not_a_verdict(tmp_path):
    seed = write_tree(tmp_path / "seed")
    (seed / IN_TREE_MANIFEST).unlink()
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)

    with pytest.raises(ComponentError) as caught:
        with after_the_manifest_check_passed():
            Tree.baseline(repo).manifest()

    assert IN_TREE_MANIFEST in str(caught.value)
