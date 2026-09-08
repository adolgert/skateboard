#!/usr/bin/env python3
"""Trusted GPU profiler wrapper used only as a disposable job entrypoint.

Trust role: the kernel count in a device-proof claim comes from the
profiler's own export, read by this wrapper as root and written to the
root-only evidence directory. The submitted program never writes the
number it is judged by.

The profiler cannot be root while the program is not: its session can be
joined only by the uid that opened it, and a program of another uid
aborts before `main` with "no permission to get access to the session".
So the profiler runs as the job uid, and what keeps its report honest is
the order of events, not file ownership. The profiler's launcher adopts
and kills the program's stragglers itself before it exits; this wrapper
then looks for anything of the job uid still alive and kills that too,
and only then reads the report. A program that left something running
past the profiler's exit has left something that could have rewritten
the report, and its profile is refused as evidence rather than read.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

JOB_UID = 65532
EVIDENCE_DIR = "/run/evidence"


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


def as_job_uid(command: list[str]) -> list[str]:
    """The same command, run as the job uid with nothing of root's kept."""
    return [
        "/usr/bin/setpriv", f"--reuid={JOB_UID}", f"--regid={JOB_UID}", "--clear-groups",
        "--", *command,
    ]


def processes_of(uid: int, proc: str = "/proc") -> list[int]:
    """Every live process whose real uid is `uid`.

    A zombie is not live: it holds no memory and runs nothing, and only
    waits for its parent to collect its exit status. This wrapper is the
    container's first process, so the profiler's own exited children
    are parented to it and would otherwise be counted forever.
    """
    found = []
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        fields = {}
        try:
            with open(os.path.join(proc, entry, "status"), encoding="utf-8") as status:
                for line in status:
                    key, _, value = line.partition(":")
                    if key in ("Uid", "State"):
                        fields[key] = value.strip()
        except OSError:
            continue
        if fields.get("State", "").startswith("Z"):
            continue
        if fields.get("Uid") and int(fields["Uid"].split()[0]) == uid:
            found.append(int(entry))
    return sorted(found)


def reap_orphans() -> None:
    """Collect every exited child so nothing stays a zombie under pid 1."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def survivors_stopped(uid: int, *, proc: str = "/proc", kill=None, attempts: int = 50) -> int:
    """Kill everything of `uid` still running, and say how many there were.

    The number is what matters: any process alive here outlived the
    profiler and could have rewritten what it wrote. Killing is done as
    the uid itself, since root in this container holds no capability
    to signal another uid's processes, and repeated until none are left
    or the attempts run out.
    """
    kill = kill or (lambda: subprocess.run(
        as_job_uid(["/usr/bin/kill", "-9", "-1"]), capture_output=True, timeout=15,
    ))
    reap_orphans()
    first = len(processes_of(uid, proc))
    remaining = first
    for _ in range(attempts):
        if not remaining:
            break
        kill()
        time.sleep(0.02)
        reap_orphans()
        remaining = len(processes_of(uid, proc))
    if remaining:
        raise RuntimeError(f"{remaining} process(es) of uid {uid} could not be stopped")
    return first


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "--" or len(argv) == 1:
        print("usage: profile_run.py -- command ...", file=sys.stderr)
        return 64
    os.chmod(EVIDENCE_DIR, 0o700)
    # The profiler writes its report here as the job uid. The directory
    # is root's and lists to nobody else; the job uid can create in it
    # and the sticky bit stops it removing or renaming what it did not
    # make. The report itself is the job uid's file, which is why the
    # sweep below decides whether it may be read.
    scratch = tempfile.mkdtemp(prefix="profile-", dir="/tmp")
    os.chmod(scratch, 0o1733)
    report = os.path.join(scratch, "profile")
    command = as_job_uid([
        "/usr/bin/env", f"HOME={scratch}",
        "nsys", "profile", "--trace=cuda,nvtx,openacc,openmp", "--sample=none",
        "--cpuctxsw=none", "--force-overwrite=true", "--output", report,
        "--", *argv[1:],
    ])
    ran = subprocess.run(command, capture_output=True, text=True)
    # Forward application diagnostics for debugging.  They are never used as
    # profiler evidence by the supervisor.
    sys.stdout.write(ran.stdout)
    sys.stderr.write(ran.stderr)
    result = {"ok": False, "kernels_launched": 0, "kernel_names": []}
    try:
        left_behind = survivors_stopped(JOB_UID)
    except RuntimeError as exc:
        left_behind = None
        result["error"] = str(exc)
    if left_behind:
        result["error"] = (
            f"the profiled program left {left_behind} process(es) running after it "
            f"exited, so its profile is not read as evidence"
        )
    elif left_behind == 0:
        report_file = report + ".nsys-rep"
        sqlite_file = os.path.join(EVIDENCE_DIR, "profile.sqlite")
        if os.path.isfile(report_file) and not os.path.islink(report_file):
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
    with open(os.path.join(EVIDENCE_DIR, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, sort_keys=True)
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
