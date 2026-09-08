"""The deployed region a gateway test stands a gateway up around.

Every one of these tests needs a region: a repository, a working copy the
agent submits from, a ledger, two strategy files, and the manifest of the
code the region belongs to. Written once here so that a test says only
what is different about its own region, and so that a field added to
RegionConfig is added in one place rather than in six.
"""
from __future__ import annotations

from pathlib import Path

from equivalent.ledger.acceptance import PORTING
from equivalent.region.config import RegionConfig

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
STRATEGY_PATH = STRATEGY_DIR / "stdpar_managed.yaml"
BASELINE_STRATEGY_PATH = STRATEGY_DIR / "cpu_reference.yaml"
ONBOARDING_STRATEGY_PATH = STRATEGY_DIR / "onboarding.yaml"
# Where a porting region's own spec lives, in the words the fixture repo
# writes it under.
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"


def region_config(
    tmp_path,
    *,
    manifest,
    repo_dir,
    region_id: str = "ch04:step",
    phase: str = PORTING,
    spec_path: str | None = SPEC_PATH,
    strategy_path: Path = STRATEGY_PATH,
    baseline_strategy_path: Path = BASELINE_STRATEGY_PATH,
    working_copy_dir=None,
    **extra,
) -> RegionConfig:
    """One deployed region, with the paths a deployment would have given it.

    `extra` is whatever this test's region says about itself that the
    others do not -- a visible dataset, a reviewed original, a pinned
    executor identity.
    """
    if working_copy_dir is None:
        working_copy_dir = tmp_path / "working"
        working_copy_dir.mkdir(exist_ok=True)
    return RegionConfig(
        region_id=region_id,
        code="tsunami",
        phase=phase,
        repo_dir=repo_dir,
        spec_path=spec_path,
        ledger_dir=tmp_path / "ledger",
        strategy_path=strategy_path,
        baseline_strategy_path=baseline_strategy_path,
        working_copy_dir=working_copy_dir,
        manifest=manifest,
        **extra,
    )
