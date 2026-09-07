#!/usr/bin/env python3
"""Trusted GPU profiler wrapper used only as a disposable job entrypoint."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys


def kernel_evidence(sqlite_file: str) -> dict:
    """What the profiler's own export says the GPU ran.

    The launch count in a device-proof claim is this number, read from
    the profiler's tables rather than from anything the measured program
    printed. A run whose export holds no kernel table has not been shown
    to have launched anything, and raises rather than counting zero.
    """
    with sqlite3.connect(sqlite_file) as db:
        count = db.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_KERNEL").fetchone()[0]
        names = db.execute(
            "SELECT DISTINCT s.value FROM CUPTI_ACTIVITY_KIND_KERNEL k "
            "JOIN StringIds s ON s.id = k.shortName ORDER BY s.value LIMIT 100"
        ).fetchall()
    return {
        "ok": True, "kernels_launched": int(count),
        "kernel_names": [row[0] for row in names],
        "collector": "nsys/CUPTI_ACTIVITY_KIND_KERNEL",
    }


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "--" or len(argv) == 1:
        print("usage: profile_run.py -- command ...", file=sys.stderr)
        return 64
    evidence_dir = "/run/evidence"
    os.chmod(evidence_dir, 0o700)
    report = os.path.join(evidence_dir, "profile")
    command = [
        "nsys", "profile", "--trace=cuda,nvtx,openacc,openmp", "--sample=none",
        "--cpuctxsw=none", "--force-overwrite=true", "--output", report,
        "/usr/bin/setpriv", "--reuid=65532", "--regid=65532", "--clear-groups",
        "--", *argv[1:],
    ]
    ran = subprocess.run(command, capture_output=True, text=True)
    # Forward application diagnostics for debugging.  They are never used as
    # profiler evidence by the supervisor.
    sys.stdout.write(ran.stdout)
    sys.stderr.write(ran.stderr)
    result = {"ok": False, "kernels_launched": 0, "kernel_names": []}
    report_file = report + ".nsys-rep"
    sqlite_file = report + ".sqlite"
    if os.path.exists(report_file):
        exported = subprocess.run(
            ["nsys", "export", "--type", "sqlite", "--force-overwrite=true",
             "--output", sqlite_file, report_file],
            capture_output=True, text=True,
        )
        if exported.returncode == 0 and os.path.exists(sqlite_file):
            try:
                result = kernel_evidence(sqlite_file)
            except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
                result["error"] = f"could not read nsys SQLite export: {exc}"
        else:
            result["error"] = "nsys could not export its protected report"
    else:
        result["error"] = "nsys produced no protected report"
    with open(os.path.join(evidence_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, sort_keys=True)
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
