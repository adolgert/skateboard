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
from pathlib import Path

from .context import CheckContext, CheckResult, failed
from .errors import ComponentError


class AnalyzerError(ComponentError):
    """The analyzer subprocess produced no usable verdict.

    This is an infrastructure failure (bad path, crash, malformed output),
    not a verdict about the region's code, so the caller must not record a
    claim for it.
    """


def _reason(item: dict) -> str:
    """One analyzer finding as the line a person reads it in.

    A control-flow finding names where it is; a finding about the spec
    itself has no line to name, so it says what is wrong instead.
    """
    if "reason" in item:
        return f"spec: {item['reason']}"
    return f"{item.get('label')}:{item.get('line')}: {str(item.get('keyword', '')).upper()}: {item.get('text')}"


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Run the strategy's analyzer against the region's current tree.

    A pass records the region's new allow-list in its detail. That list,
    and not the frozen value that was current before this check ran, is
    what the caller must use to compute the subject a later claim is
    filed against (see the fixed-point note in equivalent.gateway.app).
    """
    with ctx.tree.materialized() as scratch:
        spec_file = Path(scratch) / ctx.spec_path
        result = subprocess.run(
            [*shlex.split(ctx.strategy.analyzer_command), str(spec_file),
             "--repo-root", scratch, "--json"],
            capture_output=True, text=True,
        )
        try:
            analysis = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise AnalyzerError(
                f"analyzer produced no usable output (exit {result.returncode}): {result.stderr[-2000:]}"
            ) from exc

    if analysis["verdict"] != "pass":
        return failed(
            {
                "violations": analysis["violations"], "notes": analysis["notes"],
                "resolved_ranges": analysis.get("resolved_ranges", []),
                "range_count": analysis.get("range_count", 0),
                "total_lines": analysis.get("total_lines", 0),
            },
            [_reason(item) for item in analysis["violations"]],
        )

    candidate_globs = sorted({*analysis["src_files"], ctx.spec_path})
    outside = [g for g in candidate_globs if not ctx.strategy.allows(g)]
    if outside:
        return failed(
            {"reason": "not covered by the strategy's allow_globs", "paths": outside},
            [f"{path} is not covered by the strategy's allow_globs" for path in outside],
        )

    return CheckResult(
        verdict="pass",
        detail={
            "file_list": analysis["src_files"], "allow_globs": candidate_globs,
            "resolved_ranges": analysis.get("resolved_ranges", []),
            "range_count": analysis.get("range_count", 0),
            "total_lines": analysis.get("total_lines", 0),
        },
    )
