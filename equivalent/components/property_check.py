"""Runs a code's own module of invariants against the port's replay binary.

Trust role: what this returns becomes the regression/property claim. Every
other regression check compares a port against recorded answers, which
says a port is right on the inputs someone happened to capture. This one
lets the code state what must be true of *any* input -- mass conserved, a
symmetry respected, the same answer twice -- and then searches for an
input where the port is not. A wrong verdict here would say a port has
been searched when it has not.

The search is random, so the run is only repeatable if the seed is
recorded. A request may name one, which is how a person re-runs exactly
the search that failed; a request that names none has one drawn here and
written into the claim. The seed and the example count are the only
configuration, and because they are part of what the gateway hashes, a
repeat at the same seed is the same search and comes back as the claim
already filed, while a fresh seed is a new one.

Nothing is judged here beyond pass or fail: the builder ran pytest on the
code's own module, and what the module asserted is the code's business.
What this adds is the record of which module was run, at which seed, over
how many examples, and what the run printed -- which is where Hypothesis
writes the minimized failing example a person needs.
"""
from __future__ import annotations

import random

from equivalent.gateway.submit import attempt_id_for
from equivalent.manifest.schema import Manifest

from .errors import ComponentError

# How many examples each property draws when a request does not say. Large
# enough that a search is worth calling one, small enough that a gate stays
# a gate: every example starts a process.
DEFAULT_MAX_EXAMPLES = 100

# The width of a drawn seed. 32 bits is what fits comfortably in a claim
# and in a person's retyping of it.
SEED_BITS = 32

# How much of the run's output the claim keeps. Hypothesis prints the
# minimized falsifying example at the end, so the end is what matters.
LOG_TAIL_CHARS = 4000


def properties_module(manifest: Manifest) -> str:
    """The path of the code's property module, relative to its tree root.

    The manifest resolves it against the source tree so that the loader
    can check it is really there; the builder is given the tree-relative
    spelling, because the tree it holds is a copy at a path of its own.
    """
    return manifest.properties.relative_to(manifest.source.root).as_posix()


def run_module(builder, attempt_id: str, manifest: Manifest, cases: dict,
               *, seed=None, max_examples: int = DEFAULT_MAX_EXAMPLES) -> dict:
    """One property run and the verdict it becomes, wherever it was asked for.

    The same call and the same detail serve both the check a port faces
    and the one an onboarding session runs against the baseline: what
    differs between them is which workspace and which cases, and those are
    the caller's to name. A seed of None is drawn here and written into
    the detail, because a search nobody can repeat is not evidence.

    Returns {"verdict": "pass" | "fail", "detail": {...}}. Raises
    ComponentError if the builder could not be reached.
    """
    drawn = random.SystemRandom().getrandbits(SEED_BITS) if seed is None else int(seed)
    examples = int(max_examples)
    module = properties_module(manifest)
    replay = manifest.build.targets["replay"]

    try:
        resp = builder.properties(
            attempt_id, replay.executable, module, cases, drawn, examples,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/properties call failed: {exc}") from exc

    return {
        "verdict": "pass" if resp.get("ok") else "fail",
        "detail": {
            "module": module,
            "seed": drawn,
            "max_examples": examples,
            "passed": resp.get("passed", 0),
            "failed": resp.get("failed", 0),
            "errors": resp.get("errors", 0),
            "log_tail": (resp.get("log_tail") or "")[-LOG_TAIL_CHARS:],
        },
    }


def check(region_id: str, tree_sha: str, manifest: Manifest, visible_cases: dict, builder,
          *, seed=None, max_examples: int = DEFAULT_MAX_EXAMPLES) -> dict:
    """Run the code's properties on the submitted tree, and say what happened.

    Returns {"verdict": "pass" | "fail", "detail": {...}}. Raises
    ComponentError when there is nothing to run -- a code that declares no
    properties module, or a region with no visible dataset to draw a
    corpus from -- because neither is a statement about whether this port
    is correct.
    """
    if manifest.properties is None:
        raise ComponentError(
            f"code '{manifest.name}' declares no properties module, so there are no "
            f"invariants to run against this port"
        )
    if not visible_cases:
        raise ComponentError("no visible dataset configured for this region")

    return run_module(
        builder, attempt_id_for(region_id, tree_sha), manifest, visible_cases,
        seed=seed, max_examples=max_examples,
    )
