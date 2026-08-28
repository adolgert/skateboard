"""Wraps the SESE control-flow analyzer as a gateway component.

Trust role: what this returns becomes a claim. It runs the strategy's own
analyzer_command as a subprocess against a tree the gateway materializes
itself into a scratch directory -- never the agent's submitted working
copy, and never the shared checkout in the gateway's repo_dir -- so
nothing the subprocess does beyond its stdout can affect the claim.

Scope, settled 2026-08-27: only the mechanical SESE control-flow property
(no goto / early return / entry / stop) is checked here. The architecture
doc's "difference between spec and computed effects" -- confirming the
spec's declared footprint matches what the code actually reads and
writes -- needs real static-analysis tooling (FortranCallGraph plus a
compile step, or an equivalent) that this repository does not yet run
generically for an arbitrary region on demand. That is a separate, later
predicate, not this one.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from pathlib import Path

from equivalent.gateway.submit import materialize_tree
from equivalent.strategy.schema import Strategy

from .errors import ComponentError

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AnalyzerError(ComponentError):
    """The analyzer subprocess produced no usable verdict.

    This is an infrastructure failure (bad path, crash, malformed output),
    not a verdict about the region's code, so the caller must not record a
    claim for it.
    """


def check(repo_dir, ref: str, spec_path: str, strategy: Strategy, project_root: Path | None = None) -> dict:
    """Run the strategy's analyzer against the region's current tree.

    Returns {"verdict": "pass" | "fail", "detail": {...}, "allow_globs": [...] | None}.
    `allow_globs` is set only on a pass -- it is the region's new
    allow-list, which the caller must use to compute the subject this
    claim is filed against (see the fixed-point note in
    equivalent.gateway.app), not the frozen value that was current before
    this check ran.
    """
    project_root = project_root or DEFAULT_PROJECT_ROOT

    with tempfile.TemporaryDirectory() as scratch:
        materialize_tree(repo_dir, ref, scratch)
        spec_file = Path(scratch) / spec_path
        result = subprocess.run(
            [*shlex.split(strategy.analyzer_command), str(spec_file), "--repo-root", scratch, "--json"],
            cwd=project_root, capture_output=True, text=True,
        )
        try:
            analysis = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise AnalyzerError(
                f"analyzer produced no usable output (exit {result.returncode}): {result.stderr[-2000:]}"
            ) from exc

    if analysis["verdict"] != "pass":
        return {
            "verdict": "fail",
            "detail": {"violations": analysis["violations"], "notes": analysis["notes"]},
            "allow_globs": None,
        }

    candidate_globs = sorted({analysis["src_file"], spec_path})
    outside = [g for g in candidate_globs if not strategy.allows(g)]
    if outside:
        return {
            "verdict": "fail",
            "detail": {"reason": "not covered by the strategy's allow_globs", "paths": outside},
            "allow_globs": None,
        }

    return {
        "verdict": "pass",
        "detail": {"file_list": [analysis["src_file"]], "allow_globs": candidate_globs},
        "allow_globs": candidate_globs,
    }
