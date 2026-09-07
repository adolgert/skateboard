"""Wraps the oracle's /v1/compare as two gateway components.

regression_visible reads the outputs already stored in the tree's latest
gpu/executed claim (written by run_replay) rather than re-running the
binary.

regression_holdout is the one action that reaches two backends in a
single dispatch: it fetches the held-out inputs from the oracle, runs
them through the builder itself, and compares -- the agent has no action
that would let it see a held-out input or a held-out output on its own,
and this component never puts either into a claim's detail. The oracle
itself also never returns held-out per-case detail to anyone, by its own
design (services/oracle/app.py) -- this is defense in depth on top of that,
not the only thing enforcing it.
"""
from __future__ import annotations

from equivalent.ledger.subjects import Subject

from .context import CheckContext, CheckResult
from .errors import ComponentError
from .harness_capture import HOLDOUT

# What the oracle's answer says the bands it judged by were.
POLICY_KEY = "policy_sha256"


def _policy_material(resp: dict) -> tuple:
    """The tolerance policy that shaped a verdict, as the material it is."""
    return (Subject(kind="policy", sha256=resp[POLICY_KEY]),)


def check_visible(ctx: CheckContext, config: dict) -> CheckResult:
    run_claim = ctx.claims["gpu/executed"]
    if "outputs" not in run_claim.predicate.detail:
        raise ComponentError("the gpu/executed claim for this tree recorded no outputs")

    try:
        resp = ctx.oracle.compare(
            dataset="visible", outputs=run_claim.predicate.detail["outputs"],
        )
    except Exception as exc:
        raise ComponentError(f"oracle /v1/compare call failed: {exc}") from exc

    per_case = resp.get("per_case", {})
    return CheckResult(
        verdict=resp["verdict"],
        detail={"per_case": per_case, POLICY_KEY: resp[POLICY_KEY]},
        reasons=tuple(
            f"case '{name}' is outside the code's tolerance bands"
            for name in sorted(per_case) if not per_case[name].get("pass")
        ),
        materials=_policy_material(resp),
    )


def check_holdout(ctx: CheckContext, config: dict) -> CheckResult:
    attempt_id = ctx.provenance.attempt_id()
    replay = ctx.manifest.build.targets["replay"]
    try:
        holdout = ctx.oracle.holdout_inputs()["cases"]
        run_resp = ctx.builder.run(
            attempt_id, replay.executable, holdout,
            notify=ctx.strategy.device_proof.notify,
            mandatory=ctx.strategy.device_proof.mandatory,
        )
    except Exception as exc:
        # Transport errors can include backend response bodies. During a
        # held-out run those bodies are influenced by the submitted code.
        raise ComponentError("could not execute the held-out cases; private diagnostic withheld") from exc
    if not run_resp.get("ok"):
        raise ComponentError("held-out run failed; private diagnostic withheld")

    try:
        resp = ctx.oracle.compare(dataset=HOLDOUT, outputs=run_resp["outputs"])
    except Exception as exc:
        raise ComponentError("held-out comparison unavailable; private diagnostic withheld") from exc

    # Deliberately no outputs and no per-case detail here -- the oracle's
    # own response for holdout never includes any, and this claim's detail
    # must not become the place that leak happens through instead. The
    # same rule governs the reasons: a session is told the verdict, and
    # which held-out case failed is not something it may learn.
    return CheckResult(
        verdict=resp["verdict"],
        detail={POLICY_KEY: resp[POLICY_KEY]},
        materials=_policy_material(resp),
    )
