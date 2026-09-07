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

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from equivalent.ledger.subjects import frozen_subject, tree_subject
from equivalent.region.config import region_branch
from equivalent.region.current import matches_any, working_copy_files
from equivalent.tree import Tree, git_bytes, git_text, rev_parse


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


def _build_tree(repo_dir, files: dict[str, bytes]) -> str:
    """Write `files` as a git tree object without touching the working directory or HEAD."""
    repo_dir = Path(repo_dir)
    index_file = repo_dir / ".git" / f"tmp-index-{uuid.uuid4().hex}"
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        for path in sorted(files):
            blob = git_bytes(
                repo_dir, "hash-object", "-w", "--stdin", input=files[path], env=env,
            ).decode("ascii").strip()
            git_text(repo_dir, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        return git_text(repo_dir, "write-tree", env=env).strip()
    finally:
        index_file.unlink(missing_ok=True)


def _commit_tree_if_changed(repo_dir, branch: str, files: dict[str, bytes], message: str) -> bool:
    git_tree = _build_tree(repo_dir, files)
    old_tip = rev_parse(repo_dir, branch)
    parent = old_tip or rev_parse(repo_dir, "main")
    parent_tree = git_text(repo_dir, "rev-parse", f"{parent}^{{tree}}").strip()
    if git_tree == parent_tree:
        if old_tip is None:
            try:
                git_text(repo_dir, "update-ref", f"refs/heads/{branch}", parent, "0" * 40)
            except subprocess.CalledProcessError as exc:
                raise ConcurrentSubmissionError(
                    f"region branch {branch!r} changed during submit; retry from the current tree"
                ) from exc
        return False
    commit = git_text(repo_dir, "commit-tree", git_tree, "-p", parent, "-m", message).strip()
    # Compare-and-swap the branch.  Two gateway processes may construct
    # submissions concurrently; a late writer must not overwrite a commit it
    # never used as its parent.
    try:
        git_text(repo_dir, "update-ref", f"refs/heads/{branch}", commit, old_tip or "0" * 40)
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
        if not matches_any(path, allow_globs):
            rejected.append({"path": path, "reason": "not_allowed"})
            continue
        applied[path] = raw

    constructed = {**baseline, **applied}
    frozen_files = [{"path": p, "content": c} for p, c in baseline.items() if not matches_any(p, allow_globs)]
    tree_files = [{"path": p, "content": c} for p, c in constructed.items()]
    not_sent = sorted(
        p for p in baseline if matches_any(p, allow_globs) and p not in working
    )

    branch = region_branch(region_id)
    committed = _commit_tree_if_changed(repo_dir, branch, constructed, f"submit {region_id} session={session}")

    return SubmitReceipt(
        tree=tree_subject(tree_files).sha256,
        frozen=frozen_subject(frozen_files).sha256,
        rejected=tuple(rejected),
        not_sent=tuple(not_sent),
        committed=committed,
    )
