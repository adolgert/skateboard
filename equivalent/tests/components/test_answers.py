"""What the builder reverified about an executable, and what it matches.

A build claim names the bytes of every target it built. Before a later
action trusts that build again the builder is asked what it still holds,
and the two are compared -- so what counts as the same executable is one
statement here rather than three key lookups at the comparison.
"""
from __future__ import annotations

import pytest

from equivalent.components.answers import ArtifactsResponse, ExecutableIdentity
from equivalent.components.errors import ComponentError

TARGET = {"executable": "replay", "sha256": "a" * 64, "size": 12345}


def _reported(**overrides) -> ExecutableIdentity:
    entry = {"sha256": TARGET["sha256"], "size": TARGET["size"], "verified": True}
    return ExecutableIdentity.reported("replay", {**entry, **overrides})


def test_the_executable_a_build_claim_names_is_the_one_the_builder_still_holds():
    assert _reported().matches(TARGET)


@pytest.mark.parametrize("difference", [
    {"sha256": "b" * 64},
    {"size": 999},
    {"verified": False},
    {"verified": "yes"},
])
def test_anything_the_builder_no_longer_stands_behind_matches_nothing(difference):
    # An entry the builder did not reverify is not a weaker yes: it is the
    # builder declining to say those bytes are there.
    assert not _reported(**difference).matches(TARGET)


def test_an_artifacts_answer_is_read_as_identities_and_not_as_bare_tables():
    answer = ArtifactsResponse.parse({
        "ok": True,
        "executables": {"replay": {"sha256": "a" * 64, "size": 12345, "verified": True}},
    })

    still_there = answer.executables["replay"]
    assert still_there.executable == "replay"
    assert still_there.matches(TARGET)


def test_an_artifacts_entry_that_is_not_an_object_is_no_answer_at_all():
    with pytest.raises(ComponentError):
        ArtifactsResponse.parse({"ok": True, "executables": {"replay": "gone"}})
