"""Turns an agent's working copy into a git tree.

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
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import frozen_subject, tree_subject


@dataclass(frozen=True)
class SubmitReceipt:
    tree: str
    frozen: str
    rejected: tuple  # ({"path": ..., "reason": "not_allowed" | "binary"}, ...)
    not_sent: tuple  # allowed baseline paths absent from the working copy, named as a warning
    committed: bool


def _git(repo_dir, *args, input=None, env=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True,
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

    Mirrors what demo/orchestrator/orchestrator.py's init_repo() already
    does with demo/work/: the baseline is a plain folder of files, git-init
    once. Returns the baseline commit id.
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


def tracked_files(repo_dir, ref: str = "main") -> list[dict]:
    """Every file tracked at `ref`, as {"path": ..., "content": ...} pairs."""
    listing = _git(repo_dir, "ls-tree", "-r", "--name-only", ref)
    files = []
    for path in listing.splitlines():
        if path:
            files.append(
                {"path": path, "content": _git(repo_dir, "show", f"{ref}:{path}")}
                )
    return files


def _read_working_copy(working_copy_dir) -> dict[str, bytes]:
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


def resolve_allow_globs(store: LedgerStore, spec_path: str) -> list[str]:
    """The region's current allow-list.

    Before any passing sese/verified claim exists for the region, the
    allow-list is the spec file alone (the bootstrap rule). After one
    exists, the allow-list is whatever that claim's own detail recorded --
    the sese_check component is the thing that writes that field; this
    function only reads it.
    """
    passing = [
        c for c in store.all_claims()
        if c.predicateType == "sese/verified" and c.predicate.verdict == "pass"
    ]
    if not passing:
        return [spec_path]
    return max(passing, key=lambda c: c.ts).predicate.detail["allow_globs"]


def _build_tree(repo_dir, files: dict[str, str]) -> str:
    """Write `files` as a git tree object without touching the working directory or HEAD."""
    repo_dir = Path(repo_dir)
    index_file = repo_dir / ".git" / f"tmp-index-{uuid.uuid4().hex}"
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        for path in sorted(files):
            blob = _git(repo_dir, "hash-object", "-w", "--stdin", input=files[path], env=env).strip()
            _git(repo_dir, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        return _git(repo_dir, "write-tree", env=env).strip()
    finally:
        index_file.unlink(missing_ok=True)


def _commit_tree_if_changed(repo_dir, branch: str, files: dict[str, str], message: str) -> bool:
    git_tree = _build_tree(repo_dir, files)
    parent = _rev_parse(repo_dir, branch) or _rev_parse(repo_dir, "main")
    parent_tree = _git(repo_dir, "rev-parse", f"{parent}^{{tree}}").strip()
    if git_tree == parent_tree:
        if _rev_parse(repo_dir, branch) is None:
            _git(repo_dir, "update-ref", f"refs/heads/{branch}", parent)
        return False
    commit = _git(repo_dir, "commit-tree", git_tree, "-p", parent, "-m", message).strip()
    _git(repo_dir, "update-ref", f"refs/heads/{branch}", commit)
    return True


def submit(repo_dir, region_id: str, working_copy_dir, allow_globs: list[str], session: str) -> SubmitReceipt:
    """Lay the allowed files from `working_copy_dir` over the baseline and commit them.

    `working_copy_dir` is read directly (a mounted, read-only view of the
    agent's own working copy), not sent as request content.
    """
    baseline = {f["path"]: f["content"] for f in tracked_files(repo_dir, "main")}
    working = _read_working_copy(working_copy_dir)

    applied = {}
    rejected = []
    for path, raw in working.items():
        if not _matches_any(path, allow_globs):
            rejected.append({"path": path, "reason": "not_allowed"})
            continue
        try:
            applied[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejected.append({"path": path, "reason": "binary"})

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


def frozen_for_allow_globs(repo_dir, allow_globs: list[str]) -> str:
    """The frozen-set hash for an explicit allow-list: baseline files it doesn't cover."""
    baseline = tracked_files(repo_dir, "main")
    frozen_files = [f for f in baseline if not _matches_any(f["path"], allow_globs)]
    return frozen_subject(frozen_files).sha256


def materialize_tree(repo_dir, ref: str, dest_dir) -> None:
    """Write every file tracked at `ref` into `dest_dir`, for a subprocess to read.

    Reads through git's object store (`git show`), not the shared working
    tree in `repo_dir` -- which submit() deliberately never checks out --
    so this never depends on, or races with, whatever happens to be on
    disk in `repo_dir` itself.
    """
    dest_dir = Path(dest_dir)
    for f in tracked_files(repo_dir, ref):
        path = dest_dir / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"])


def attempt_id_for(region_id: str, tree_sha: str) -> str:
    """A stable workspace key the builder can reuse across build/run/sanitize/time.

    demo/builder/stages.py keeps a workspace on disk per attempt_id and
    never checks a tree hash itself; deriving the id from (region, tree)
    means every action against the same tree reuses the same workspace
    without the gateway needing to remember anything extra. This follows
    the builder's real, stateful behavior; if the builder loses its
    workspace (a container restart), re-running the build re-creates it
    under the same id.
    """
    safe_region = re.sub(r"[^A-Za-z0-9._-]", "-", region_id)
    return f"{safe_region}-{tree_sha[:16]}"


def fortran_files_at(repo_dir, ref: str) -> list[dict]:
    """Every .f90/.F90 file tracked at `ref`, sorted by path.

    Sent as-is to the builder: demo/builder/stages.py picks its own fixed
    basenames out of whatever it's given and compiles them in its own
    fixed order, so neither the set's precision nor its order matters here
    -- only that everything the builder might want is present. Sending
    this superset is simpler than deriving a precise per-region
    dependency closure, and the builder ignores what it doesn't use.
    """
    files = [f for f in tracked_files(repo_dir, ref) if f["path"].lower().endswith(".f90")]
    return sorted(files, key=lambda f: f["path"])


def baseline_commit(repo_dir) -> str | None:
    """The baseline commit id -- the `main` branch's tip -- or None if the repo isn't initialized."""
    return _rev_parse(repo_dir, "main")


def baseline_tree_sha(repo_dir) -> str:
    """The pristine baseline's own tree hash -- what timing/baseline is filed against."""
    return tree_subject(tracked_files(repo_dir, "main")).sha256


def current_tree_and_frozen(repo_dir, region_id: str, store: LedgerStore, spec_path: str) -> tuple[str, str]:
    """The region's current tree and frozen-set hashes, read straight from the gateway repo.

    Before the region's first submit, the branch doesn't exist yet and the
    current tree is just the baseline itself. This never runs `submit()`
    and never writes anything; it only reads what is already there.
    """
    ref = current_ref(repo_dir, region_id)
    allow_globs = resolve_allow_globs(store, spec_path)
    return (
        tree_subject(tracked_files(repo_dir, ref)).sha256,
        frozen_for_allow_globs(repo_dir, allow_globs),
    )
