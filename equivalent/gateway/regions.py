"""Where one region's files, spec, and ledger live.

Trust role: none by itself. A wrong path here points every check at the
wrong repository or the wrong ledger; that is a person's configuration
mistake to catch, not something this module can validate on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegionConfig:
    region_id: str
    repo_dir: Path
    spec_path: str
    ledger_dir: Path
    strategy_path: Path
    # The directory the gateway reads a submit from. It is named here,
    # not in the submit request, so that nothing the agent sends can
    # choose which gateway-side path gets read.
    working_copy_dir: Path
    visible_dataset_dir: Path | None = None
