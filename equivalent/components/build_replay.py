"""Wraps the builder's /v1/build as a gateway component (Step 6b).

Trust role: what this returns becomes a claim. The builder is trusted for
what it measures (it runs agent code); this component only carries files
to it and turns its response into a verdict. It never reads the agent's
own working copy -- only the gateway's own committed tree.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for, fortran_files_at
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def check(repo_dir, ref: str, region_id: str, tree_sha: str, strategy: Strategy, builder) -> dict:
    """Build the region's current tree under the strategy's profile.

    Returns {"verdict": "pass" | "fail", "detail": {...}}. Raises
    ComponentError if the builder call itself couldn't be completed (not a
    verdict about the code).
    """
    files = fortran_files_at(repo_dir, ref)
    if not files:
        raise ComponentError(f"no .f90 files found in tree {tree_sha} at ref {ref}")

    attempt_id = attempt_id_for(region_id, tree_sha)
    payload = [{"path": f["path"], "content": f["content"]} for f in files]
    try:
        resp = builder.build(attempt_id, payload, strategy.name)
    except Exception as exc:
        raise ComponentError(f"builder /v1/build call failed: {exc}") from exc

    if not resp.get("ok"):
        return {
            "verdict": "fail",
            "detail": {"stage": resp.get("stage"), "target": resp.get("target"), "log_tail": resp.get("log_tail", "")},
        }
    return {
        "verdict": "pass",
        "detail": {"attempt_id": attempt_id, "minfo_excerpt": resp.get("minfo_excerpt", ""), "log_tail": resp.get("log_tail", "")},
    }
