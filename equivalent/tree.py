"""The files git tracks at one ref, as a single value.

Trust role: every claim is about a tree, and a tree is exactly the files
git holds at one ref -- so whatever a check reads has to be those files
and nothing else. Reading is done through git's object store (`git
ls-tree`, `git show`), never through the working directory in
`repo_dir`, which the gateway deliberately never checks out. That way
what a check sees never depends on, or races with, whatever happens to
be on disk in the repository itself.

Content is carried as bytes from the moment it is read: a code's tree
holds namelists and small data files that are not UTF-8, and
re-encoding one of them would make the tree hash describe something
nobody submitted.

This sits below both the gateway and the components. The gateway
mutates repositories and resolves allow-lists; a component only reads a
tree, and reads it from here.

Making the repository and asking it for the baseline commit are here for
the same reason: they are facts about the repository itself, with no
opinion about what a submission means, and both a gateway building a
submission and a CLI reading a ledger need them.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import weakref
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from equivalent.ledger.subjects import tree_subject
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_tree_manifest


def git_bytes(repo_dir, *args, input=None, env=None) -> bytes:
    """git's own output, undecoded: file content is not always text."""
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True,
        input=input, env=env, check=True,
    ).stdout


def git_text(repo_dir, *args, input=None, env=None) -> str:
    """The same, decoded: for git's own words -- ids, refs, messages."""
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True,
        input=input, env=env, check=True,
    ).stdout


def rev_parse(repo_dir, ref) -> str | None:
    """The commit `ref` names, or None if the repository has no such ref."""
    r = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo_dir,
        capture_output=True,
        text=True,
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
    git_text(repo_dir, "init", "-q")
    git_text(repo_dir, "config", "user.email", "gateway@equivalent")
    git_text(repo_dir, "config", "user.name", "gateway")
    git_text(repo_dir, "add", "-A")
    git_text(repo_dir, "commit", "-q", "-m", "baseline")
    git_text(repo_dir, "branch", "-M", "main")
    return git_text(repo_dir, "rev-parse", "HEAD").strip()


def baseline_commit(repo_dir) -> str | None:
    """The baseline commit id -- the `main` branch's tip -- or None if the repo isn't initialized."""
    return rev_parse(repo_dir, "main")


@dataclass(frozen=True)
class Tree:
    """The files tracked at `ref` in the git repository at `repo_dir`.

    A Tree is one value, not a handle: it is the files as they were when
    it first read them. A ref that can move (a region branch, `main`) is
    resolved to content once, so two checks holding the same Tree are
    talking about the same files even if a submit lands between them. To
    see a later state, make another Tree.
    """

    repo_dir: str | Path
    ref: str = "main"

    @classmethod
    def baseline(cls, repo_dir) -> "Tree":
        """The pristine baseline -- `main`, what the code was before any port."""
        return cls(repo_dir, "main")

    @cached_property
    def files(self) -> dict[str, bytes]:
        """Every tracked file, as {path: bytes}, read once for this Tree's life.

        The listing is asked for NUL-separated (`-z`) so that a path git
        would otherwise quote comes back as the bytes it really is, and
        the content is read without decoding.
        """
        listing = git_bytes(self.repo_dir, "ls-tree", "-r", "--name-only", "-z", self.ref)
        files = {}
        for raw in listing.split(b"\0"):
            if raw:
                path = raw.decode("utf-8")
                files[path] = git_bytes(self.repo_dir, "show", f"{self.ref}:{path}")
        return files

    @cached_property
    def sha(self) -> str:
        """The tree hash claims are filed against."""
        return tree_subject(self.file_list()).sha256

    def file_list(self) -> list[dict]:
        """The same files as {"path", "content"} pairs, which is what hashing takes."""
        return [{"path": path, "content": content} for path, content in sorted(self.files.items())]

    def payload(self) -> list[dict]:
        """Every tracked file as {"path", "b64"} pairs sorted by path, for the builder.

        The builder is sent the whole tree, not a filtered list of source
        files. Its build is the tree's own makefile, which reads include
        files, namelists, and small data files that no extension test would
        recognize -- and which the code's manifest is under no obligation to
        call source. Filtering here would produce a tree that does not build
        for reasons nobody could see.

        Content is base64 rather than text because the request body is JSON
        and a real code's tree is not all UTF-8.
        """
        return [
            {"path": path, "b64": base64.b64encode(content).decode("ascii")}
            for path, content in sorted(self.files.items())
        ]

    def write_to(self, dest_dir) -> None:
        """Write every tracked file into `dest_dir`, for a subprocess to read."""
        dest_dir = Path(dest_dir)
        for path, content in self.files.items():
            out = dest_dir / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)

    @cached_property
    def directory(self) -> Path:
        """A directory holding these files, for a subprocess to read.

        A Tree's content never changes, so the copy is made once and kept
        for as long as the Tree is alive rather than rebuilt per call: a
        single check needs the tree on disk several times over (the
        manifest, the policy the manifest names, an analyzer subprocess),
        and each rebuild would be another walk of every file. Keeping it
        also means a path the manifest carries still points at something
        after the code that loaded it has returned. The directory goes
        away when the Tree does, which is what `weakref.finalize` is for;
        nothing here holds the Tree alive.
        """
        scratch = Path(tempfile.mkdtemp(prefix="equivalent-tree-"))
        weakref.finalize(self, shutil.rmtree, scratch, True)
        self.write_to(scratch)
        return scratch

    def manifest(self):
        """The manifest this tree carries, loaded from a copy of it.

        While a code is being brought in, its manifest lives inside the
        tree rather than beside it: the agent is writing that file along
        with the makefile and the drivers it names. Every onboarding check
        after the manifest check is described by it -- which targets to
        build, which arguments make which dataset, what the region's
        variables are, what the timing run writes -- so they all read it
        from here rather than each loading it their own way.

        Whatever went wrong reading it is raised as one ValueError,
        because to a caller there is one fact: this tree's manifest is not
        readable. What that means -- an error rather than a verdict about
        the code -- is the components layer's to say.
        """
        try:
            return load_tree_manifest(self.directory)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"the tree's own manifest at {IN_TREE_MANIFEST} did not load: {exc}"
            ) from exc

    def manifest_and_policy(self) -> tuple:
        """This tree's manifest and the bytes of the tolerance policy it names.

        The policy lives inside the tree, at a path the manifest names, so
        a check that needs the bands a port is judged within asks for both
        here rather than opening the manifest's path against nothing.

        The bytes are returned rather than the parsed policy so that the
        caller can hash exactly what it read, which is what makes the
        policy subject on its claim the same subject as on any other claim
        reached under the same file.
        """
        manifest = self.manifest()
        try:
            return manifest, Path(manifest.tolerances).read_bytes()
        except OSError as exc:
            raise OSError(
                f"the tolerance policy the tree's manifest names could not be read: {exc}"
            ) from exc
