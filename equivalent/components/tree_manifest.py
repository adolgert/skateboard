"""The manifest a submitted tree carries, read once for the checks that need it.

While a code is being brought in, its manifest lives inside the tree
rather than beside it: the agent is writing that file along with the
makefile and the drivers it names. Every onboarding check after the
manifest check is described by it -- which targets to build, which
arguments make which dataset, what the region's variables are, what the
timing run writes -- so they all read it from here rather than each
materializing the tree and loading it their own way.

Trust role: what this returns tells the checks after it what to run and
what to expect. It decides nothing itself; a manifest that says the
wrong thing is caught by the manifest check, which has already passed
against this same tree before anything here is called. So a manifest
that will not load at this point is not the agent's mistake but
something wrong on the harness's side, and it is raised as an error
rather than recorded as a verdict about the code.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from equivalent.gateway.submit import materialize_tree
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_tree_manifest

from .errors import ComponentError


def _load(scratch):
    try:
        return load_tree_manifest(scratch)
    except (OSError, ValueError) as exc:
        raise ComponentError(
            f"the tree's own manifest at {IN_TREE_MANIFEST} did not load, although a "
            f"passing manifest claim for this tree says it did: {exc}"
        ) from exc


def manifest_of(repo_dir, ref: str):
    """The manifest of the tree at `ref`, loaded from a scratch copy of it."""
    with tempfile.TemporaryDirectory() as scratch:
        materialize_tree(repo_dir, ref, scratch)
        return _load(scratch)


def manifest_and_policy(repo_dir, ref: str) -> tuple:
    """The tree's manifest and the bytes of the tolerance policy it names.

    The two are read in one materialization because the policy lives
    inside the tree, at a path the manifest names: once the scratch copy
    is gone, the path the manifest carries points at nothing. A check
    that needs the bands a port is judged within therefore asks for both
    here rather than opening the manifest's path afterwards.

    The bytes are returned rather than the parsed policy so that the
    caller can hash exactly what it read, which is what makes the policy
    subject on its claim the same subject as on any other claim reached
    under the same file.
    """
    with tempfile.TemporaryDirectory() as scratch:
        materialize_tree(repo_dir, ref, scratch)
        manifest = _load(scratch)
        try:
            return manifest, Path(manifest.tolerances).read_bytes()
        except OSError as exc:
            raise ComponentError(
                f"the tolerance policy the tree's manifest names could not be read, "
                f"although a passing manifest claim for this tree says it was: {exc}"
            ) from exc
