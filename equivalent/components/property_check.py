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

The builder's protected execution trace must observe the bound replay
executable at least once. Pytest's counts still come from the submitted
process and `max_examples` is a Hypothesis ceiling rather than an observed
count, so the claim records both facts separately. What the module asserted
remains the code owner's business.
"""
from __future__ import annotations

import random

from equivalent.manifest.schema import Manifest
from equivalent.tree import attempt_id_for

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
    if isinstance(max_examples, bool):
        raise ComponentError("property max_examples must be a positive integer")
    try:
        examples = int(max_examples)
    except (TypeError, ValueError) as exc:
        raise ComponentError("property max_examples must be a positive integer") from exc
    if examples <= 0:
        raise ComponentError("property max_examples must be a positive integer")
    drawn = random.SystemRandom().getrandbits(SEED_BITS) if seed is None else int(seed)
    module = properties_module(manifest)
    replay = manifest.build.targets["replay"]

    try:
        resp = builder.properties(
            attempt_id, replay.executable, module, cases, drawn, examples,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/properties call failed: {exc}") from exc

    problems = []
    if resp.get("stage") != "properties":
        problems.append(f"builder returned stage {resp.get('stage')!r}, expected 'properties'")
    if resp.get("seed") != drawn:
        problems.append("builder returned a different seed from the property run requested")
    if resp.get("max_examples") != examples:
        problems.append(
            "builder returned a different max_examples from the property run requested"
        )
    if not isinstance(resp.get("ok"), bool):
        problems.append("builder returned no boolean property outcome")

    counts = {}
    for name in (
        "passed", "failed", "errors", "skipped", "deselected", "xfailed", "xpassed",
        "collected", "executed",
    ):
        value = resp.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"builder returned an invalid {name} count")
            counts[name] = 0
        else:
            counts[name] = value

    successful = (
        counts["passed"] > 0
        and counts["failed"] == 0
        and counts["errors"] == 0
        and counts["skipped"] == 0
        and counts["deselected"] == 0
        and counts["xfailed"] == 0
        and counts["xpassed"] == 0
        and counts["collected"] == counts["passed"]
        and counts["executed"] == counts["passed"]
    )
    if counts["passed"] == 0:
        problems.append("no property test passed")
    if counts["skipped"]:
        problems.append(f"{counts['skipped']} property test(s) were skipped")
    expected_collected = sum(
        counts[name]
        for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
    )
    expected_executed = sum(
        counts[name] for name in ("passed", "failed", "errors", "xfailed", "xpassed")
    )
    if counts["collected"] != expected_collected or counts["executed"] != expected_executed:
        problems.append("builder's property collection counts are internally inconsistent")
    if counts["deselected"]:
        problems.append(f"{counts['deselected']} property test(s) were deselected")
    for name in ("xfailed", "xpassed"):
        if counts[name]:
            problems.append(f"{counts[name]} property test(s) were {name}")
    replays_observed = resp.get("replays_observed")
    if (
        isinstance(replays_observed, bool)
        or not isinstance(replays_observed, int)
        or replays_observed <= 0
    ):
        problems.append(
            "protected execution evidence observed no invocation of the bound replay executable"
        )
    if resp.get("ok") is not successful:
        problems.append("builder's property outcome is inconsistent with its test counts")

    detail = {
        "module": module,
        "seed": drawn,
        "max_examples": examples,
        **counts,
        "replays_observed": replays_observed,
        "counts_source": resp.get("counts_source"),
        "log_tail": (resp.get("log_tail") or "")[-LOG_TAIL_CHARS:],
    }
    if "executable_identity" in resp:
        detail["executable_identity"] = resp["executable_identity"]
    if problems:
        detail["problems"] = problems
    return {"verdict": "pass" if not problems and successful else "fail", "detail": detail}


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
