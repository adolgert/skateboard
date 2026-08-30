"""Runs a code's own invariants against the baseline, while it is being brought in.

Trust role: what this returns becomes a claim. Running the code's
property module here, on the baseline build, answers a question a port's
own property claim cannot: do these invariants hold of the code as it
already is? A module that fails on the baseline fails on every port of
it, and would be read as the port's fault; catching that now is the
whole reason this runs during onboarding.

A code that declares no property module still files a claim, and it
passes. The absence is recorded on purpose: `properties: null` in the
manifest is a statement the code makes about itself, and a person
reading the ledger should find it written down rather than find a row
nobody filed and have to guess which it was.

The run itself is the same call a port's property check makes, and the
same detail comes back -- one function serves both, so the seed, the
example count, and what the run printed mean the same thing in either
claim.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for_strategy
from equivalent.ledger.capture_sets import load_capture_set
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy

from . import harness_capture, harness_replay, property_check, tree_manifest
from .errors import ComponentError

# The dataset the properties draw their corpus from: the one the agent
# can see. Held-out inputs are for judging a port, not for a search the
# agent is running.
VISIBLE = "visible"

# What the detail says when the code states no invariants, in the words
# the manifest writes it in.
NO_PROPERTIES = (
    "the manifest says `properties: null`: this code states no invariants that hold "
    "for every input, so there is nothing here to search. Recorded so that the "
    "absence is a fact in the ledger rather than a check nobody ran."
)


def check(store: LedgerStore, tree: Subject, repo_dir, ref: str, region_id: str, tree_sha: str,
          baseline_strategy: Strategy, builder, *, seed=None,
          max_examples: int = property_check.DEFAULT_MAX_EXAMPLES) -> dict:
    """Run the tree's property module on the baseline build, or record that it has none.

    Returns {"verdict": "pass" | "fail", "detail": {...}}: the module that
    was run, the seed it was run at, how many examples were drawn, and
    what the run printed -- which is where the minimized failing example
    is. Raises ComponentError if the tree has no passing capture claim or
    the builder could not be reached.
    """
    manifest = tree_manifest.manifest_of(repo_dir, ref)
    if manifest.properties is None:
        return {"verdict": "pass", "detail": {"module": None, "note": NO_PROPERTIES}}

    sets = harness_capture.captured_sets(store, tree)
    if VISIBLE not in sets:
        raise ComponentError(
            f"the capture claim for tree {tree.sha256} names no '{VISIBLE}' dataset, so "
            f"there is no corpus for the code's properties to draw from"
        )
    cases = load_capture_set(store, sets[VISIBLE])

    return property_check.run_module(
        builder,
        attempt_id_for_strategy(region_id, tree_sha, baseline_strategy.name),
        manifest,
        harness_replay.wire_inputs(cases),
        seed=seed,
        max_examples=max_examples,
    )
