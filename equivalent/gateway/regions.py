"""Where one region's files, spec, and ledger live.

Trust role: none by itself. A wrong path here points every check at the
wrong repository or the wrong ledger; that is a person's configuration
mistake to catch, not something this module can validate on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from equivalent.manifest.schema import Manifest


@dataclass(frozen=True)
class RegionConfig:
    region_id: str
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
