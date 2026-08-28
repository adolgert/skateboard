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

from equivalent.gateway.submit import materialize_tree
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_tree_manifest

from .errors import ComponentError


def manifest_of(repo_dir, ref: str):
    """The manifest of the tree at `ref`, loaded from a scratch copy of it."""
    with tempfile.TemporaryDirectory() as scratch:
        materialize_tree(repo_dir, ref, scratch)
        try:
            return load_tree_manifest(scratch)
        except (OSError, ValueError) as exc:
            raise ComponentError(
                f"the tree's own manifest at {IN_TREE_MANIFEST} did not load, although a "
                f"passing manifest claim for this tree says it did: {exc}"
            ) from exc
