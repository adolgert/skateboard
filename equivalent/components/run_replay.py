"""Wraps the builder's /v1/run against the visible cases (Step 6c).

The visible outputs are stored in this claim's own detail -- regression_visible
(6e) reads them back from here rather than re-running the replay binary a
second time for the same cases, matching what
demo/orchestrator/orchestrator.py already does (it reuses the same
in-memory `runr["outputs"]`).
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def check(region_id: str, tree_sha: str, strategy: Strategy, visible_cases: dict, builder) -> dict:
    if not visible_cases:
        raise ComponentError("no visible dataset configured for this region")

    attempt_id = attempt_id_for(region_id, tree_sha)
    try:
        resp = builder.run(attempt_id, strategy.name, visible_cases, mandatory=strategy.device_proof.mandatory)
    except Exception as exc:
        raise ComponentError(f"builder /v1/run call failed: {exc}") from exc

    if not resp.get("ok"):
        return {"verdict": "fail", "detail": {"log_tail": resp.get("log_tail", "")}}

    kernels = resp.get("kernels_launched", 0)
    if kernels <= 0:
        return {
            "verdict": "fail",
            "detail": {
                "kernels_launched": 0,
                "hint": "code compiled but no GPU kernel launched; loops must be do concurrent / omp target for nvfortran to offload them",
            },
        }
    return {"verdict": "pass", "detail": {"kernels_launched": kernels, "outputs": resp["outputs"]}}
