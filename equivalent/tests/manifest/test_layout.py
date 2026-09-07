"""The one line that says which of a code's two places a manifest describes."""
import pytest

from equivalent.manifest.layout import in_tree_manifest_text, promoted_manifest_text


def test_the_two_rewrites_of_the_source_root_are_each_other_backwards():
    # The walkthrough writes the in-tree form from the promoted one and
    # promote writes the promoted form back, so the two directions are one
    # rule read twice.
    promoted = "version: 1\nsource:\n  # where the code is\n  root: baseline\n  patterns: []\n"

    in_tree = in_tree_manifest_text(promoted)

    assert "  root: .\n" in in_tree
    assert "  # where the code is\n" in in_tree
    assert promoted_manifest_text(in_tree) == promoted


def test_a_manifest_with_no_source_block_of_its_own_is_refused():
    with pytest.raises(ValueError) as raised:
        promoted_manifest_text("{version: 1, source: {root: ., patterns: []}}\n")

    assert "root: ." in str(raised.value)
