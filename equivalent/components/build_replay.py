"""Wraps the builder's /v1/build as a gateway component.

Trust role: what this returns becomes a claim. The builder is trusted for
what it measures (it runs agent code); this component only carries files
to it and turns its response into a verdict. It never reads the agent's
own working copy -- only the gateway's own committed tree.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for, source_files_at
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def check(
    repo_dir, ref: str, region_id: str, tree_sha: str,
    strategy: Strategy, manifest: Manifest, builder,
) -> dict:
    """Build the region's current tree with the strategy's own flags.

    The flags come from the strategy file, not the builder's profile
    table -- the hashed strategy YAML is the one source of what gets
    compiled. The builder echoes back what it actually passed to the
    compiler, and that is what goes into the claim's detail.

    Returns {"verdict": "pass" | "fail", "detail": {...}}. Raises
    ComponentError if the builder call itself couldn't be completed (not a
    verdict about the code).
    """
    try:
        files = source_files_at(repo_dir, ref, manifest)
    except ValueError as exc:
        # A source file in the tree that is not UTF-8: the builder is sent
        # JSON, so there is nothing to compile. A fact about the tree, not
        # a verdict about the port, so no claim is recorded.
        raise ComponentError(str(exc)) from exc
    if not files:
        raise ComponentError(
            f"no file in tree {tree_sha} at ref {ref} matches the source patterns "
            f"of code '{manifest.name}'"
        )

    fortran = strategy.languages.get("fortran")
    if fortran is None:
        raise ComponentError(f"strategy '{strategy.name}' defines no fortran language entry")

    attempt_id = attempt_id_for(region_id, tree_sha)
    payload = [{"path": f["path"], "content": f["content"]} for f in files]
    try:
        resp = builder.build(
            attempt_id, payload, strategy.name,
            flags=list(fortran.flags), link_flags=list(strategy.link_flags),
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/build call failed: {exc}") from exc

    if not resp.get("ok"):
        return {
            "verdict": "fail",
            "detail": {
                "stage": resp.get("stage"), "target": resp.get("target"),
                "flags": resp.get("flags"), "log_tail": resp.get("log_tail", ""),
            },
        }
    return {
        "verdict": "pass",
        "detail": {
            "attempt_id": attempt_id, "flags": resp.get("flags"),
            "minfo_excerpt": resp.get("minfo_excerpt", ""), "log_tail": resp.get("log_tail", ""),
        },
    }
