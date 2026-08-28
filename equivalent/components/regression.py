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
design (demo/oracle/app.py) -- this is defense in depth on top of that,
not the only thing enforcing it.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def check_visible(store: LedgerStore, tree: Subject, oracle) -> dict:
    run_claim = store.latest("gpu/executed", tree)
    if run_claim is None or run_claim.predicate.verdict != "pass" or "outputs" not in run_claim.predicate.detail:
        raise ComponentError("no passing gpu/executed claim with recorded outputs for this tree")

    try:
        resp = oracle.compare(dataset="visible", outputs=run_claim.predicate.detail["outputs"])
    except Exception as exc:
        raise ComponentError(f"oracle /v1/compare call failed: {exc}") from exc

    return {
        "verdict": resp["verdict"],
        "detail": {"per_case": resp.get("per_case", {}), "policy_sha256": resp["policy_sha256"]},
    }


def check_holdout(region_id: str, tree_sha: str, strategy: Strategy, oracle, builder) -> dict:
    attempt_id = attempt_id_for(region_id, tree_sha)
    try:
        holdout = oracle.holdout_inputs()["cases"]
        run_resp = builder.run(attempt_id, strategy.name, holdout)
    except Exception as exc:
        raise ComponentError(f"could not execute the held-out cases: {exc}") from exc
    if not run_resp.get("ok"):
        raise ComponentError(f"held-out run failed: {run_resp.get('log_tail', '')}")

    try:
        resp = oracle.compare(dataset="holdout", outputs=run_resp["outputs"])
    except Exception as exc:
        raise ComponentError(f"oracle /v1/compare call failed: {exc}") from exc

    # Deliberately no outputs and no per-case detail here -- the oracle's
    # own response for holdout never includes any, and this claim's detail
    # must not become the place that leak happens through instead.
    return {"verdict": resp["verdict"], "detail": {"policy_sha256": resp["policy_sha256"]}}
