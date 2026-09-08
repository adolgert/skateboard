"""What the builder's directory for one set of actions is called.

The builder keeps a workspace per name and never checks a tree hash
itself, so two things that must never share a directory must never share
a name: two trees of one region, two strategies over one tree, and the
region's own submission against the baseline or the reviewed original it
is compared with.
"""
from __future__ import annotations

from equivalent.components.workspaces import (
    attempt_id_for,
    attempt_id_for_strategy,
    attempt_id_for_tree,
)

REGION = "code:a/b"


def test_attempt_ids_bind_the_full_tree_and_unambiguous_region_identity():
    common_prefix = "a" * 63
    first_tree = common_prefix + "1"
    second_tree = common_prefix + "2"

    first = attempt_id_for(REGION, first_tree)
    second = attempt_id_for("code:a?b", first_tree)

    assert first_tree in first
    assert attempt_id_for(REGION, second_tree) != first
    # Both ids have the same filesystem-safe spelling of the region, so
    # their region digest is what keeps their builder workspaces distinct.
    assert second != first


def test_a_trees_purpose_keeps_it_out_of_the_regions_own_workspace():
    tree = "b" * 64

    submitted = attempt_id_for(REGION, tree)
    baseline = attempt_id_for_tree(REGION, "baseline", tree)
    original = attempt_id_for_tree(REGION, "original", tree)

    assert len({submitted, baseline, original}) == 3


def test_a_purpose_can_still_be_built_under_two_strategies():
    tree = "c" * 64

    one = attempt_id_for_tree(REGION, "original", tree, "cpu_reference")
    other = attempt_id_for_tree(REGION, "original", tree, "onboarding")

    assert one != other
    assert one == attempt_id_for_strategy(f"{REGION}-original", tree, "cpu_reference")
