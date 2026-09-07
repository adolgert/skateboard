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
"""
from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from equivalent.ledger.subjects import tree_subject
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_tree_manifest


def _git_bytes(repo_dir, *args) -> bytes:
    """git's own output, undecoded: file content is not always text."""
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, check=True,
    ).stdout


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
        listing = _git_bytes(self.repo_dir, "ls-tree", "-r", "--name-only", "-z", self.ref)
        files = {}
        for raw in listing.split(b"\0"):
            if raw:
                path = raw.decode("utf-8")
                files[path] = _git_bytes(self.repo_dir, "show", f"{self.ref}:{path}")
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
    def _scratch(self) -> Path:
        """The one directory this Tree's files are written to.

        A Tree's content never changes, so the copy is made once and kept
        for as long as the Tree is alive rather than rebuilt per call: a
        single check materializes the tree several times over (the
        manifest, the policy the manifest names, an analyzer subprocess),
        and each rebuild would be another walk of every file. Keeping it
        also means a path the manifest carries still points at something
        after the block that loaded it ends. The directory goes away when
        the Tree does, which is what `weakref.finalize` is for; nothing
        here holds the Tree alive.
        """
        scratch = Path(tempfile.mkdtemp(prefix="equivalent-tree-"))
        weakref.finalize(self, shutil.rmtree, scratch, True)
        self.write_to(scratch)
        return scratch

    @contextmanager
    def materialized(self):
        """A directory holding these files, for a subprocess to read."""
        yield self._scratch

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
        with self.materialized() as scratch:
            try:
                return load_tree_manifest(scratch)
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


def attempt_id_for_strategy(region_id: str, tree_sha: str, strategy_name: str) -> str:
    """The workspace key for building one tree with one named strategy.

    Onboarding builds the same tree twice, once per strategy, and each
    build needs its own workspace on the builder -- two builds sharing one
    would leave the second reading the first's object files. Every later
    onboarding step that wants one of those builds derives the same key
    from the same three things rather than being handed it.
    """
    safe_strategy = re.sub(r"[^A-Za-z0-9._-]", "-", strategy_name)[:32]
    strategy_digest = hashlib.sha256(strategy_name.encode("utf-8")).hexdigest()[:12]
    return f"{attempt_id_for(region_id, tree_sha)}-{safe_strategy}-{strategy_digest}"


def attempt_id_for(region_id: str, tree_sha: str) -> str:
    """A stable workspace key the builder can reuse across build/run/sanitize/time.

    services/builder/stages.py keeps a workspace on disk per attempt_id and
    never checks a tree hash itself; deriving the id from (region, tree)
    means every action against the same tree reuses the same workspace
    without the gateway needing to remember anything extra. This follows
    the builder's real, stateful behavior; if the builder loses its
    workspace (a container restart), re-running the build re-creates it
    under the same id.
    """
    safe_region = re.sub(r"[^A-Za-z0-9._-]", "-", region_id)[:48]
    region_digest = hashlib.sha256(region_id.encode("utf-8")).hexdigest()[:16]
    return f"{safe_region}-{region_digest}-{tree_sha}"
