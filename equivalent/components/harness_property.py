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

from . import harness_capture, harness_replay, property_check
from .context import CheckContext, CheckResult
from .errors import ComponentError
# The properties draw their corpus from the dataset the agent can see.
# Held-out inputs are for judging a port, not for a search the agent is
# running.
from .names import VISIBLE

# What the detail says when the code states no invariants, in the words
# the manifest writes it in.
NO_PROPERTIES = (
    "the manifest says `properties: null`: this code states no invariants that hold "
    "for every input, so there is nothing here to search. Recorded so that the "
    "absence is a fact in the ledger rather than a check nobody ran."
)


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Run the tree's property module on the baseline build, or record that it has none.

    A seed the request names is the same search again, and the gateway's
    config hash carries it, so a repeat at that seed comes back as the
    claim already filed. A request that names none has one drawn in the
    run and written into the claim, which is how a person reads back the
    search that failed and asks for it again.

    The detail names the module that was run, the seed it was run at, how
    many examples were drawn, and what the run printed -- which is where
    the minimized failing example is. Raises ComponentError if the builder
    could not be reached.
    """
    manifest = ctx.provenance.manifest()
    if manifest.properties is None:
        return CheckResult(verdict="pass", detail={"module": None, "note": NO_PROPERTIES})

    sets = harness_capture.captured_sets(ctx)
    if VISIBLE not in sets:
        raise ComponentError(
            f"the capture claim for tree {ctx.tree.sha} names no '{VISIBLE}' dataset, so "
            f"there is no corpus for the code's properties to draw from"
        )
    cases = ctx.sets.load(sets[VISIBLE])

    return property_check.run_module(
        ctx.builder,
        ctx.provenance.attempt_id(),
        manifest,
        harness_replay.wire_inputs(cases),
        seed=config.get("seed"),
        max_examples=config.get("max_examples", property_check.DEFAULT_MAX_EXAMPLES),
    )
