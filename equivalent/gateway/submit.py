"""Turns an agent's working copy into a git tree.

Trust role: the tree this builds is the subject of every claim about a
submission, so what it holds has to be exactly what the agent wrote.
File content is carried as bytes from the moment it is read to the
moment it is written back out -- a code's tree holds namelists and small
data files that are not UTF-8, and re-encoding one of them would make the
tree hash describe something nobody submitted.

The gateway keeps one git repository per baseline. Each region gets its
own branch inside it, `region/<id>` (`:` is not legal in a git branch
name, so it is replaced with `-`). Commits are built with git's plumbing
commands (`hash-object`, `update-index`, `write-tree`, `commit-tree`)
against a throwaway index file, never by checking out the branch. That
way concurrent submits for different regions never fight over one working
directory.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from equivalent.ledger.acceptance import ONBOARDING
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import frozen_subject, tree_subject
from equivalent.tree import Tree


@dataclass(frozen=True)
class SubmitReceipt:
    tree: str
    frozen: str
    # ({"path": ..., "reason": "not_allowed"}, ...). Being outside the
    # allow-list is the only reason a file is turned away; what is in it
    # never is.
    rejected: tuple
    not_sent: tuple  # allowed baseline paths absent from the working copy, named as a warning
    committed: bool


class ConcurrentSubmissionError(RuntimeError):
    """The region branch advanced while this submission was constructed."""


def _git(repo_dir, *args, input=None, env=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True,
        input=input, env=env, check=True,
    ).stdout


def _git_bytes(repo_dir, *args, input=None, env=None) -> bytes:
    """The same, without decoding: for file content, which is not always text."""
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True,
        input=input, env=env, check=True,
    ).stdout


def _rev_parse(repo_dir, ref) -> str | None:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo_dir,
        capture_output=True,
        text=True
        )
    return r.stdout.strip() if r.returncode == 0 else None


def init_baseline_repo(repo_dir, seed_dir) -> str:
    """Copy `seed_dir` into a fresh git repo at `repo_dir` and commit it once.

    The baseline is a plain folder of files, git-init once. Returns the
    baseline commit id.
    """
    repo_dir = Path(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-r", f"{seed_dir}/.", f"{repo_dir}/"], check=True)
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "gateway@equivalent")
    _git(repo_dir, "config", "user.name", "gateway")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "baseline")
    _git(repo_dir, "branch", "-M", "main")
    return _git(repo_dir, "rev-parse", "HEAD").strip()


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


def _matches_any(path: str, globs: list[str]) -> bool:
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


def _build_tree(repo_dir, files: dict[str, bytes]) -> str:
    """Write `files` as a git tree object without touching the working directory or HEAD."""
    repo_dir = Path(repo_dir)
    index_file = repo_dir / ".git" / f"tmp-index-{uuid.uuid4().hex}"
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        for path in sorted(files):
            blob = _git_bytes(
                repo_dir, "hash-object", "-w", "--stdin", input=files[path], env=env,
            ).decode("ascii").strip()
            _git(repo_dir, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        return _git(repo_dir, "write-tree", env=env).strip()
    finally:
        index_file.unlink(missing_ok=True)


def _commit_tree_if_changed(repo_dir, branch: str, files: dict[str, bytes], message: str) -> bool:
    git_tree = _build_tree(repo_dir, files)
    old_tip = _rev_parse(repo_dir, branch)
    parent = old_tip or _rev_parse(repo_dir, "main")
    parent_tree = _git(repo_dir, "rev-parse", f"{parent}^{{tree}}").strip()
    if git_tree == parent_tree:
        if old_tip is None:
            try:
                _git(repo_dir, "update-ref", f"refs/heads/{branch}", parent, "0" * 40)
            except subprocess.CalledProcessError as exc:
                raise ConcurrentSubmissionError(
                    f"region branch {branch!r} changed during submit; retry from the current tree"
                ) from exc
        return False
    commit = _git(repo_dir, "commit-tree", git_tree, "-p", parent, "-m", message).strip()
    # Compare-and-swap the branch.  Two gateway processes may construct
    # submissions concurrently; a late writer must not overwrite a commit it
    # never used as its parent.
    try:
        _git(repo_dir, "update-ref", f"refs/heads/{branch}", commit, old_tip or "0" * 40)
    except subprocess.CalledProcessError as exc:
        raise ConcurrentSubmissionError(
            f"region branch {branch!r} changed during submit; retry from the current tree"
        ) from exc
    return True


def submit(repo_dir, region_id: str, working_copy_dir, allow_globs: list[str], session: str) -> SubmitReceipt:
    """Lay the allowed files from `working_copy_dir` over the baseline and commit them.

    `working_copy_dir` is read directly (a mounted, read-only view of the
    agent's own working copy), not sent as request content.
    """
    baseline = Tree.baseline(repo_dir).files
    working = working_copy_files(working_copy_dir)

    applied = {}
    rejected = []
    for path, raw in working.items():
        if not _matches_any(path, allow_globs):
            rejected.append({"path": path, "reason": "not_allowed"})
            continue
        applied[path] = raw

    constructed = {**baseline, **applied}
    frozen_files = [{"path": p, "content": c} for p, c in baseline.items() if not _matches_any(p, allow_globs)]
    tree_files = [{"path": p, "content": c} for p, c in constructed.items()]
    not_sent = sorted(
        p for p in baseline if _matches_any(p, allow_globs) and p not in working
    )

    branch = _region_branch(region_id)
    committed = _commit_tree_if_changed(repo_dir, branch, constructed, f"submit {region_id} session={session}")

    return SubmitReceipt(
        tree=tree_subject(tree_files).sha256,
        frozen=frozen_subject(frozen_files).sha256,
        rejected=tuple(rejected),
        not_sent=tuple(not_sent),
        committed=committed,
    )


def region_slug(region_id: str) -> str:
    """A region id spelled so it can be a path or branch component.

    A region id like "ch04:step" contains a colon, which git does not
    accept in a branch name and which is awkward in a directory name.
    Replacing it with a dash is the one rule; the region's branch and its
    ledger directory both use this so the two never disagree.
    """
    return region_id.replace(":", "-")


def _region_branch(region_id: str) -> str:
    return f"region/{region_slug(region_id)}"


def current_ref(repo_dir, region_id: str) -> str:
    """The region's branch if it has one yet, else "main" (before its first submit)."""
    branch = _region_branch(region_id)
    return branch if _rev_parse(repo_dir, branch) is not None else "main"


def current_commit(repo_dir, region_id: str) -> str:
    """Resolve the mutable region ref once to an immutable commit id."""
    ref = current_ref(repo_dir, region_id)
    commit = _rev_parse(repo_dir, ref)
    if commit is None:
        raise ValueError(f"cannot resolve current ref {ref!r} for region {region_id!r}")
    return commit


def frozen_for_allow_globs(repo_dir, allow_globs: list[str]) -> str:
    """The frozen-set hash for an explicit allow-list: baseline files it doesn't cover."""
    frozen_files = [
        f for f in Tree.baseline(repo_dir).file_list()
        if not _matches_any(f["path"], allow_globs)
    ]
    return frozen_subject(frozen_files).sha256


def baseline_commit(repo_dir) -> str | None:
    """The baseline commit id -- the `main` branch's tip -- or None if the repo isn't initialized."""
    return _rev_parse(repo_dir, "main")


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
