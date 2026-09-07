"""The exception every component raises, and the one wrapper that raises it.

A component raises this when it cannot produce a real verdict
(a network failure, a crash, output it cannot parse, a
precondition it expected but didn't find) -- as opposed to running
successfully and reporting pass or fail, which is a real verdict and
becomes a claim. The gateway must never turn a ComponentError into a
claim; it logs the request outcome as "error" and returns {"error": ...}.
"""
from __future__ import annotations

from contextlib import contextmanager


class ComponentError(Exception):
    pass


@contextmanager
def after_the_manifest_check_passed():
    """Turn a failed read of the tree into an error, never into a verdict.

    Every check that runs after the manifest check reads the tree's own
    manifest, and the tolerance policy that manifest names, to learn what
    to run and what to expect. The manifest check has already passed
    against this same tree before any of them is called, so a file that
    will not load now is not the agent's mistake but something wrong on
    the harness's side.
    """
    try:
        yield
    except (OSError, ValueError) as exc:
        raise ComponentError(
            f"{exc}; a passing manifest claim for this tree already read it, "
            f"so this is a fault on the harness's side"
        ) from exc
