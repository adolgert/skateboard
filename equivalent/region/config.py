"""Where one region's files, spec, and ledger live, and what it is called.

Trust role: none by itself. A wrong path here points every check at the
wrong repository or the wrong ledger; that is a person's configuration
mistake to catch, not something this module can validate on its own.

The naming rule sits here beside the paths because a region id is used
in two places -- the branch its submits go to and the directory its
ledger is filed under -- and the two disagreeing would file evidence
about one region under the name of another.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from equivalent.manifest.schema import Manifest


@dataclass(frozen=True)
class RegionConfig:
    region_id: str
    # Which code this region belongs to, as the deployment's own
    # configuration names it. It is the name of the code's directory
    # under `programs`, which is where anything written about the code as
    # a whole -- rather than about one submission -- belongs.
    code: str
    # Which kind of session this region is for: bringing a code in, or
    # porting a region of one that has been brought in. It decides which
    # action rows the region has, which requirement list its status is
    # judged by, and how its allow-list is arrived at.
    phase: str
    repo_dir: Path
    # The region spec the analyzer reads. A region being onboarded has
    # none: nothing about a single region has been decided yet, and its
    # allow-list comes from the strategy rather than from a spec.
    spec_path: str | None
    ledger_dir: Path
    strategy_path: Path
    # The strategy the pristine baseline is built with, so a speedup
    # compares this port against a stated floor rather than against
    # whatever the builder happened to default to.
    baseline_strategy_path: Path
    # The directory the gateway reads a submit from. It is named here,
    # not in the submit request, so that nothing the agent sends can
    # choose which gateway-side path gets read.
    working_copy_dir: Path
    # The manifest of the code this region belongs to, already loaded. It
    # is carried here rather than looked up per request so that every
    # claim a session files names one and the same description of the
    # code, and so that a manifest edited on disk mid-session cannot
    # change what the running gateway believes.
    manifest: Manifest
    visible_dataset_dir: Path | None = None
    # Human-reviewed pre-onboarding snapshot, outside the submitted tree.
    original_reference_path: Path | None = None
    executor_identity: str | None = None
    oracle_identity: str | None = None


def region_slug(region_id: str) -> str:
    """A region id spelled so it can be a path or branch component.

    A region id like "ch04:step" contains a colon, which git does not
    accept in a branch name and which is awkward in a directory name.
    Replacing it with a dash is the one rule; the region's branch and its
    ledger directory both use this so the two never disagree.
    """
    return region_id.replace(":", "-")


def region_branch(region_id: str) -> str:
    """The branch in the gateway's repository that holds this region's submits."""
    return f"region/{region_slug(region_id)}"
