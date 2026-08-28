"""Wraps the builder's /v1/time as two gateway components (Step 6f).

time_port times the region's own current tree. It relies on build_replay
(6b) having already built the timing binary in the same builder
workspace -- build_replay sends every .f90 file in the tree, a superset
that already includes whatever demo/builder/stages.py needs for its
end-to-end timing target, so there is nothing extra to build here.

time_baseline measures the pristine baseline instead, which the region's
own build_replay call never touches -- so this component does its own
build first, using demo's fixed "cpu_best" profile (a comparison floor,
not a strategy choice; ACTION_TABLE's time_baseline row has no `requires`
for a build step, so this bundles one in rather than changing the table).
The resulting claim is filed against the baseline tree, not whatever tree
happens to be current for the region.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for, fortran_files_at

from .errors import ComponentError

BASELINE_PROFILE = "cpu_best"


def _time(builder, attempt_id: str, repeats: int) -> dict:
    try:
        resp = builder.time(attempt_id, repeats)
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc
    if not resp.get("ok"):
        return {"verdict": "fail", "detail": {"log_tail": resp.get("log_tail", "")}}
    return {"verdict": "pass", "detail": {"runs_s": resp["runs_s"], "gpu_exclusive": resp.get("gpu_exclusive")}}


def check_port(region_id: str, tree_sha: str, builder, repeats: int = 5) -> dict:
    return _time(builder, attempt_id_for(region_id, tree_sha), repeats)


def check_baseline(repo_dir, region_id: str, baseline_tree_sha: str, builder, repeats: int = 5) -> dict:
    attempt_id = attempt_id_for(f"{region_id}-baseline", baseline_tree_sha)
    files = fortran_files_at(repo_dir, "main")
    if not files:
        raise ComponentError(f"no .f90 files found in the baseline tree {baseline_tree_sha}")
    payload = [{"path": f["path"], "content": f["content"]} for f in files]
    try:
        build_resp = builder.build(attempt_id, payload, BASELINE_PROFILE)
    except Exception as exc:
        raise ComponentError(f"builder /v1/build call failed: {exc}") from exc
    if not build_resp.get("ok"):
        return {"verdict": "fail", "detail": {"stage": "build", "log_tail": build_resp.get("log_tail", "")}}
    return _time(builder, attempt_id, repeats)
