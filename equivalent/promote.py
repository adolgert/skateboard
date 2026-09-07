"""Copies what an onboarding session proved into the code's own directory.

Trust role: this is the step where evidence becomes deployment. What it
writes is what every later porting session is checked against -- the
manifest the gateway loads, the baseline the repository is seeded from,
the datasets the agent is given, and the captures the oracle answers
from. So it refuses far more than it does.

Three things have to be true before anything is written, and each is a
refusal naming what was wrong rather than a warning:

- the region is one being onboarded, because a porting region's tree is
  a port and not a description of a code;
- the region's current tree has a passing claim for every onboarding
  requirement, read from the ledger by the same computation `status`
  prints, so what is promoted is what a person read as ONBOARDED;
- the working copy is that tree, file for file, byte for byte. The
  person reviews the working copy -- it is the directory they can open
  -- so promoting anything else would deploy code nobody read.

Nothing here judges the code. Every verdict was reached by the gateway
and is already in the ledger; this reads those claims and copies files.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from equivalent.components import harness_capture
from equivalent.ledger.acceptance import FINISHED_WORD, ONBOARDING, requirements_for
from equivalent.ledger.capture_sets import load_capture_set, write_dataset
from equivalent.ledger.evidence import required_materials_by_predicate
from equivalent.ledger.status import compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.layout import (
    BASELINE_DIR,
    CAPTURES_DIR,
    DATASETS_DIR,
    MANIFEST_NAME,
    promoted_manifest_text,
)
from equivalent.manifest.schema import IN_TREE_MANIFEST
from equivalent.region.config import RegionConfig
from equivalent.region.current import (
    current_commit,
    current_tree_and_frozen,
    working_copy_files,
)
from equivalent.region.deployment import GatewayConfig
from equivalent.region.evidence import evidence_materials_for
from equivalent.strategy.schema import load_strategy
from equivalent.tree import Tree


class PromoteRefused(Exception):
    """Something was not as promoting requires, and nothing was written."""


@dataclass(frozen=True)
class PromotedSet:
    """One capture set, and where under the code's directory it is written."""

    relative: str  # under the code's directory
    sha256: str
    inputs: bool
    outputs: bool


def first_difference(tree: dict, working: dict) -> str | None:
    """The first path where these two sets of files disagree, in path order."""
    for path in sorted(set(tree) | set(working)):
        if path not in working:
            return f"{path} is in the tree that passed and not in the working copy"
        if path not in tree:
            return f"{path} is in the working copy and not in the tree that passed"
        if tree[path] != working[path]:
            return f"{path} differs between the working copy and the tree that passed"
    return None


def promoted_sets(sets: dict) -> list[PromotedSet]:
    """Where each stored capture set is written under the code's directory.

    The visible set is the one that is split: the agent is given its
    inputs and the oracle keeps its answers, which is what makes a
    regression check a question rather than a lookup. Every other set --
    the held-out one, and anything else the manifest declared -- stays
    whole on the oracle's side.

    The program's own outputs are not among these. A whole-program run at
    a real timing size writes megabytes, and the run a port is compared
    against is the deployment's own `time_baseline` run, kept in the
    region's ledger -- so promoting one would check a large file into the
    repository that nothing later reads.
    """
    promoted = []
    for name in sorted(sets):
        if name == harness_capture.VISIBLE:
            promoted.append(PromotedSet(
                f"{DATASETS_DIR}/{name}", sets[name], inputs=True, outputs=False,
            ))
            promoted.append(PromotedSet(
                f"{CAPTURES_DIR}/{name}", sets[name], inputs=False, outputs=True,
            ))
        else:
            promoted.append(PromotedSet(
                f"{CAPTURES_DIR}/{name}", sets[name], inputs=True, outputs=True,
            ))
    return promoted


def _occupied(paths: list[Path]) -> list[Path]:
    """Of these destinations, the ones that already hold something."""
    return [
        path for path in paths
        if path.is_file() or (path.is_dir() and any(path.iterdir()))
    ]


def _clear(paths: list[Path]) -> int:
    """Remove these destinations, and say how many files went with them."""
    removed = 0
    for path in paths:
        if path.is_dir():
            removed += sum(1 for child in path.rglob("*") if child.is_file())
            shutil.rmtree(path)
        elif path.is_file():
            removed += 1
            path.unlink()
    return removed


def _missing_rows(status: dict) -> list[str]:
    return [row["predicateType"] for row in status["rows"] if row["status"] != "present"]


def _onboarded_tree(cfg: RegionConfig, store: LedgerStore) -> tuple[str, Subject]:
    """The region's current tree, refused unless every onboarding claim passed."""
    ref = current_commit(cfg.repo_dir, cfg.region_id)
    materials = evidence_materials_for(cfg)
    store.activate_context(materials)
    tree_sha, frozen_sha = current_tree_and_frozen(
        cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase,
        load_strategy(cfg.strategy_path),
        required_materials=materials, ref=ref,
    )
    tree = Subject(kind="tree", sha256=tree_sha)
    status = compute_status(
        store, requirements_for(ONBOARDING), ONBOARDING,
        tree=tree, frozen=Subject(kind="frozen", sha256=frozen_sha),
        required_materials=materials,
        required_materials_by_predicate=required_materials_by_predicate(
            store, requirements_for(ONBOARDING), ONBOARDING, tree, materials,
        ),
    )
    if not status["accepted"]:
        raise PromoteRefused(
            f"region '{cfg.region_id}' is not {FINISHED_WORD[ONBOARDING]} on tree "
            f"{tree_sha}: it is still missing {_missing_rows(status)}"
        )
    return ref, tree


def _reviewed_tree(cfg: RegionConfig, ref: str) -> dict:
    """The files of the tree that passed, refused unless the working copy is them."""
    tree = Tree(cfg.repo_dir, ref).files
    difference = first_difference(tree, working_copy_files(cfg.working_copy_dir))
    if difference is not None:
        raise PromoteRefused(
            f"the working copy {cfg.working_copy_dir} is not the tree that passed: "
            f"{difference}. What is promoted has to be what was reviewed, so submit "
            f"the working copy and run the checks again, or restore it to the tree "
            f"that passed"
        )
    return tree


def _lines(code_dir: Path, tree: dict, sets: list[PromotedSet], removed: int) -> list[str]:
    """What was written, and what the person does with it."""
    written = [
        f"wrote {code_dir}/{MANIFEST_NAME}",
        f"wrote {code_dir}/{BASELINE_DIR}/ ({len(tree) - 1} files)",
    ]
    written.extend(f"wrote {code_dir}/{promoted.relative}/" for promoted in sets)
    if removed:
        written.insert(0, f"removed {removed} file(s) that were already there")
    return written


def _next_steps(code_dir: Path, code: str) -> list[str]:
    """The steps that are a person's, spelled as the commands they are."""
    return [
        "",
        "next, by hand:",
        f"  git add {code_dir} && git commit -m 'onboard {code}'",
        f"  add a region for '{code}' to deploy/gateway.<code>.yaml with phase: porting, a",
        "    spec_path, a strategy, a baseline_strategy, and visible_dataset: visible",
        f"  set EQUIVALENT_CODE={code} in deploy/.env",
        "  cd deploy && ./down.sh && ./up.sh",
        "    -- the oracle bakes the captures in, so it has to be built again",
    ]


def promote(config: GatewayConfig, cfg: RegionConfig, programs=None, replace: bool = False) -> list[str]:
    """Write the region's onboarded tree into its code's directory.

    Returns the lines to print. Raises PromoteRefused, having written
    nothing, when the region is not one being onboarded, when its tree is
    not onboarded, when the working copy is not that tree, when the
    tree's manifest cannot be rewritten a line at a time, or when a
    destination already holds something and `replace` was not asked for.
    """
    if cfg.phase != ONBOARDING:
        raise PromoteRefused(
            f"region '{cfg.region_id}' has phase '{cfg.phase}'; promoting is what ends "
            f"an onboarding session, and a porting region's tree is a port of a code "
            f"rather than a description of one"
        )

    store = LedgerStore(cfg.ledger_dir)
    if not cfg.executor_identity:
        raise PromoteRefused(
            "promotion requires a reviewed executor_identity in the region configuration; "
            "qualify the deployment and pin its builder /healthz identity first"
        )
    ref, subject = _onboarded_tree(cfg, store)
    tree = _reviewed_tree(cfg, ref)

    if IN_TREE_MANIFEST not in tree:
        raise PromoteRefused(f"the tree that passed holds no manifest at {IN_TREE_MANIFEST}")
    try:
        manifest_text = promoted_manifest_text(tree[IN_TREE_MANIFEST].decode("utf-8"))
    except ValueError as exc:
        raise PromoteRefused(str(exc)) from exc

    sets = promoted_sets(harness_capture.captured_sets(store, subject))

    code_dir = Path(config.paths.programs if programs is None else programs) / cfg.code
    destinations = [
        code_dir / MANIFEST_NAME,
        code_dir / BASELINE_DIR,
        *(code_dir / promoted.relative for promoted in sets),
    ]
    occupied = _occupied(destinations)
    if occupied and not replace:
        raise PromoteRefused(
            "these already hold something, and promoting would write over them: "
            + ", ".join(str(path) for path in occupied)
            + ". Pass --replace to empty them first"
        )
    removed = _clear(occupied)

    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / MANIFEST_NAME).write_text(manifest_text)
    for path, content in tree.items():
        if path == IN_TREE_MANIFEST:
            # Beside the code the manifest is the code's own description;
            # a second copy inside the baseline would be one more file
            # that can disagree with it.
            continue
        destination = code_dir / BASELINE_DIR / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    for promoted in sets:
        write_dataset(
            code_dir / promoted.relative, load_capture_set(store, promoted.sha256),
            inputs=promoted.inputs, outputs=promoted.outputs,
        )

    return [*_lines(code_dir, tree, sets, removed), *_next_steps(code_dir, cfg.code)]
