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

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from equivalent.components import harness_capture
from equivalent.gateway.config import DATASETS_DIR, GatewayConfig
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import (
    current_ref,
    current_tree_and_frozen,
    tracked_files,
    working_copy_files,
)
from equivalent.ledger.acceptance import FINISHED_WORD, ONBOARDING, requirements_for
from equivalent.ledger.capture_sets import load_capture_set, write_dataset
from equivalent.ledger.status import compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import IN_TREE_MANIFEST, IN_TREE_SOURCE_ROOT
from equivalent.strategy.schema import load_strategy

# Where a code keeps the answers a port is compared against, under its
# own directory. The oracle spells this too, in its own words: it cannot
# import this package, and that is the price of it being sealed.
CAPTURES_DIR = "captures"
# What a code's own manifest is called beside its directory. The
# deployment's configuration may name any path under `programs`, but this
# is where promoting puts one, and it is what the seed script looks for.
MANIFEST_NAME = "manifest.yaml"
# What the promoted manifest says its source root is: the directory the
# tree is written into, beside the manifest.
BASELINE_DIR = "baseline"

# The `source:` mapping of a manifest, and a line inside it saying what
# the source root is. The rewrite is a line replacement rather than a
# YAML round trip because a manifest is a file a person wrote and will
# read again: loading and dumping it would silently discard every
# comment in it and re-order what is left.
SOURCE_KEY = re.compile(r"^source:\s*(#.*)?$")
TOP_LEVEL = re.compile(r"^\S")


class PromoteRefused(Exception):
    """Something was not as promoting requires, and nothing was written."""


@dataclass(frozen=True)
class PromotedSet:
    """One capture set, and where under the code's directory it is written."""

    relative: str  # under the code's directory
    sha256: str
    inputs: bool
    outputs: bool


def _root_line(value: str) -> re.Pattern:
    """A line of a manifest that says the source root is this, and nothing else."""
    return re.compile(rf"^(\s*)root:\s*{re.escape(value)}\s*$")


def _source_block(lines: list[str], expected: str) -> range:
    """The lines the manifest's top-level `source:` mapping spans."""
    start = None
    for number, line in enumerate(lines):
        if start is None:
            if SOURCE_KEY.match(line):
                start = number + 1
        elif line.strip() and TOP_LEVEL.match(line):
            return range(start, number)
    if start is None:
        raise PromoteRefused(
            f"the manifest at {IN_TREE_MANIFEST} has no `source:` section written as a "
            f"block of its own; promoting rewrites one line, so spell it as `source:` "
            f"with `root: {expected}` on a line of its own beneath it"
        )
    return range(start, len(lines))


def _rewrite_source_root(text: str, expected: str, replacement: str) -> str:
    """The same manifest with its source root changed, and everything else untouched.

    Exactly one line inside `source:` may say the root, and it may say
    nothing else. A manifest written any other way -- the root on a flow
    mapping, two `root:` keys, a comment on the same line -- is refused
    rather than guessed at, because a wrong guess here would promote a
    manifest pointing at a tree that is not the one beside it.
    """
    lines = text.splitlines(keepends=True)
    pattern = _root_line(expected)
    found = [n for n in _source_block(lines, expected) if pattern.match(lines[n])]
    if len(found) != 1:
        raise PromoteRefused(
            f"the manifest at {IN_TREE_MANIFEST} has {len(found)} lines inside `source:` "
            f"saying `root: {expected}`, and promoting rewrites exactly one; spell it as "
            f"`root: {expected}` on a line of its own, with any comment on the line above"
        )
    indent = pattern.match(lines[found[0]]).group(1)
    lines[found[0]] = f"{indent}root: {replacement}\n"
    return "".join(lines)


def promoted_manifest_text(tree_text: str) -> str:
    """The tree's manifest as it is written beside the code: source root `baseline`."""
    return _rewrite_source_root(tree_text, IN_TREE_SOURCE_ROOT, BASELINE_DIR)


def in_tree_manifest_text(promoted_text: str) -> str:
    """The code's manifest as a tree being onboarded carries it: source root `.`.

    The other direction of the same rewrite, for whoever is writing the
    manifest into a working copy: it is the same file with the same
    comments, saying that its source is the tree it sits in.
    """
    return _rewrite_source_root(promoted_text, BASELINE_DIR, IN_TREE_SOURCE_ROOT)


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
    ref = current_ref(cfg.repo_dir, cfg.region_id)
    tree_sha, frozen_sha = current_tree_and_frozen(
        cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase,
        load_strategy(cfg.strategy_path),
    )
    tree = Subject(kind="tree", sha256=tree_sha)
    status = compute_status(
        store, requirements_for(ONBOARDING), ONBOARDING,
        tree=tree, frozen=Subject(kind="frozen", sha256=frozen_sha),
    )
    if not status["accepted"]:
        raise PromoteRefused(
            f"region '{cfg.region_id}' is not {FINISHED_WORD[ONBOARDING]} on tree "
            f"{tree_sha}: it is still missing {_missing_rows(status)}"
        )
    return ref, tree


def _reviewed_tree(cfg: RegionConfig, ref: str) -> dict:
    """The files of the tree that passed, refused unless the working copy is them."""
    tree = {f["path"]: f["content"] for f in tracked_files(cfg.repo_dir, ref)}
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
    ref, subject = _onboarded_tree(cfg, store)
    tree = _reviewed_tree(cfg, ref)

    if IN_TREE_MANIFEST not in tree:
        raise PromoteRefused(f"the tree that passed holds no manifest at {IN_TREE_MANIFEST}")
    manifest_text = promoted_manifest_text(tree[IN_TREE_MANIFEST].decode("utf-8"))

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
