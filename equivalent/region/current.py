"""What a region is right now: the files in the agent's working copy,
which of them may change, and the tree and frozen-set hashes that say
what the repository holds for it.

Trust role: the allow-list is the reviewed ceiling on a port. Widening
it lets an edit reach a file nobody agreed could move, and the frozen-set
hash is what makes that visible afterwards -- it is the hash of exactly
the baseline files the allow-list does not cover, so a claim reached
under a wider list is a claim about a different frozen set and does not
stand in for one reached under the reviewed one.

Everything here only reads. The repository is read through its object
store, no branch is written, and nothing is submitted; that is why a CLI
reading a ledger on the host can ask the same questions the gateway asks
without the gateway being installed.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from equivalent.ledger.acceptance import ONBOARDING
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import frozen_subject
from equivalent.region.config import region_branch
from equivalent.tree import Tree, rev_parse


def working_copy_files(working_copy_dir) -> dict[str, bytes]:
    """Every file in the agent's working copy, as {path: bytes}.

    What counts as a file of the working copy is decided here and read
    from here by everyone: a submit lays these over the baseline, and
    `promote` asks whether these are the tree that passed. Two readers
    with two ideas of which files count would let a promotion write
    something the person never reviewed.
    """
    working_copy_dir = Path(working_copy_dir)
    out = {}
    for p in sorted(working_copy_dir.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(working_copy_dir)
        if rel.parts[0] == ".git":
            continue
        out[str(rel).replace(os.sep, "/")] = p.read_bytes()
    return out


def matches_any(path: str, globs: list[str]) -> bool:
    """Whether this path is covered by any pattern in the allow-list."""
    return any(fnmatch.fnmatch(path, g) for g in globs)


def resolve_allow_globs(
    store: LedgerStore, spec_path, phase: str, strategy, *, required_materials=(),
) -> list[str]:
    """The region's current allow-list.

    While a code is being onboarded there is no region and no SESE claim:
    the agent is rewriting the build, the drivers, and the manifest, and
    the strategy's own `allow_globs` is the whole of the answer.

    While a region is being ported the list narrows. Before any current-policy
    passing sese/verified claim exists it is the spec file alone (the
    bootstrap rule). After one exists it is whatever that claim's own detail
    recorded, after checking those paths remain inside the strategy's reviewed
    ceiling. The SESE verdict itself is candidate-tree scoped and must be rerun
    after an edit; its reviewed allow-list remains the authority that permits
    that edit to be submitted.

    The phase and the strategy are passed in rather than read from a
    configuration file here, so this stays a function of what it is
    given and the caller stays the one place that knows the region.
    """
    if phase == ONBOARDING:
        return list(strategy.allow_globs)
    passing = [
        c for c in store.all_claims()
        if c.predicateType == "sese/verified" and c.predicate.verdict == "pass"
        and store.claim_matches_context(c, required_materials)
        and isinstance(c.predicate.detail.get("allow_globs"), list)
        and spec_path in c.predicate.detail["allow_globs"]
        and all(
            isinstance(pattern, str) and strategy.allows(pattern)
            for pattern in c.predicate.detail["allow_globs"]
        )
    ]
    if not passing:
        return [spec_path]
    # Stable sorting makes the last appended claim win timestamp ties.
    return sorted(passing, key=lambda c: c.ts)[-1].predicate.detail["allow_globs"]


def current_ref(repo_dir, region_id: str) -> str:
    """The region's branch if it has one yet, else "main" (before its first submit)."""
    branch = region_branch(region_id)
    return branch if rev_parse(repo_dir, branch) is not None else "main"


def current_commit(repo_dir, region_id: str) -> str:
    """Resolve the mutable region ref once to an immutable commit id."""
    ref = current_ref(repo_dir, region_id)
    commit = rev_parse(repo_dir, ref)
    if commit is None:
        raise ValueError(f"cannot resolve current ref {ref!r} for region {region_id!r}")
    return commit


def frozen_for_allow_globs(repo_dir, allow_globs: list[str]) -> str:
    """The frozen-set hash for an explicit allow-list: baseline files it doesn't cover."""
    frozen_files = [
        f for f in Tree.baseline(repo_dir).file_list()
        if not matches_any(f["path"], allow_globs)
    ]
    return frozen_subject(frozen_files).sha256


def current_tree_and_frozen(
    repo_dir, region_id: str, store: LedgerStore, spec_path, phase: str, strategy,
    *, required_materials=(), ref: str | None = None,
) -> tuple[str, str]:
    """The region's current tree and frozen-set hashes, read straight from the gateway repo.

    Before the region's first submit, the branch doesn't exist yet and the
    current tree is just the baseline itself. This never runs `submit()`
    and never writes anything; it only reads what is already there.

    An onboarding region whose strategy allows the whole tree has an empty
    frozen set, and that is the honest answer: nothing about the code is
    being held still while it is brought in.
    """
    ref = ref or current_commit(repo_dir, region_id)
    allow_globs = resolve_allow_globs(
        store, spec_path, phase, strategy, required_materials=required_materials,
    )
    return (
        Tree(repo_dir, ref).sha,
        frozen_for_allow_globs(repo_dir, allow_globs),
    )
