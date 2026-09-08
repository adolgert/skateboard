"""Repeated timing runs and declared output collection."""
import base64
import os
import subprocess
import time

from .contract import TimeResponse
from .workspace import ExecutionFailed, path_inside

def time_run(workspace, executable, args=(), env=None, outputs=(), repeats=5,
             budget_s=300, expected_outputs=None) -> TimeResponse:
    """Time the code's own program at the size its manifest declares.

    The arguments, the environment, the files the run is expected to
    write, and the per-run budget all come from the manifest, so the
    problem size is data rather than something compiled into a source
    file. The declared output files come back with the timings: they are
    what says the fast run was also a correct one.

    They are collected once per run, not once at the end, and the
    declared files are cleared before each run -- so a caller can ask
    whether the program wrote the same thing every time, which is a
    question a single collection at the end cannot answer.
    """
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        return TimeResponse.failure("timing repeats must be a positive integer")
    program, identity = workspace.executable(executable)
    if program is None:
        return TimeResponse.failure(
            f"the tree holds no executable '{executable}'; build it first"
        )

    run_env = {**os.environ, **{str(k): str(v) for k, v in (env or {}).items()}}
    if expected_outputs is not None and set(expected_outputs) != set(outputs):
        return TimeResponse.failure(
            "expected timing outputs do not exactly match the declared outputs",
            executable_identity=identity,
        )
    # honest timing wants exclusive GPU
    gpu_excl = _gpu_exclusive()
    runs = []
    collected = []
    last = ""
    for repetition in range(repeats):
        # The immutable binary is executed from the frozen tree, and the
        # application is given a fresh copy of that tree as its working
        # directory: ordinary relative input, config and scratch files work,
        # and nothing a run writes can reach the measured binary.
        run_dir = workspace.run_directory(f"run-{repetition + 1:04d}")
        # A file left by an earlier run, or by an earlier attempt, would
        # otherwise be collected as if this run had written it.
        for relative in outputs:
            path = path_inside(run_dir, relative)
            if path is None:
                return TimeResponse.failure(
                    f"declared timing output '{relative}' leaves the tree",
                    runs_s=runs, outputs=collected,
                )
            if path == path_inside(run_dir, executable):
                return TimeResponse.failure(
                    f"declared timing output '{relative}' is the measured executable",
                    runs_s=runs, outputs=collected,
                )
            if os.path.lexists(path):
                os.unlink(path)

        t0 = time.monotonic()
        try:
            # Relative application paths resolve in the disposable copy;
            # the executable path still names the frozen original.
            job = workspace.execute(
                [program, *args], cwd=run_dir, env=run_env, timeout=budget_s,
                gpu=True, what="a timing run",
            )
        except ExecutionFailed as exc:
            return TimeResponse.failure(str(exc), runs_s=runs, outputs=collected)
        runs.append(time.monotonic() - t0)
        last = (job.stdout + job.stderr)[-1500:]
        if job.returncode != 0:
            return TimeResponse.failure(last, runs_s=runs, outputs=collected)
        if not workspace.matches(program, identity):
            return TimeResponse.failure(
                "the executable changed while it was being measured",
                runs_s=runs, outputs=collected, executable_identity=identity,
            )

        this_run = {}
        for relative in outputs:
            path = path_inside(run_dir, relative)
            if path is None or os.path.islink(path) or not os.path.isfile(path):
                return TimeResponse.failure(
                    f"run {len(runs)} wrote no '{relative}', which the manifest declares",
                    runs_s=runs, outputs=collected,
                )
            with open(path, "rb") as f:
                written = f.read()
            this_run[relative] = base64.b64encode(written).decode()
            if expected_outputs is not None and this_run[relative] != expected_outputs.get(relative):
                return TimeResponse.failure(
                    f"run {len(runs)} wrote unexpected bytes to '{relative}'",
                    runs_s=runs, outputs=[*collected, this_run],
                    executable_identity=identity,
                )
        collected.append(this_run)

    return TimeResponse(
        ok=True, runs_s=runs, gpu_exclusive=gpu_excl, outputs=collected,
        stdout_tail=last, executable_identity=identity,
    )


def _gpu_exclusive():
    # This queries the trusted supervisor's GPU view; it does not execute any
    # submitted command and therefore must not enter a disposable attempt job.
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
        rc, out = p.returncode, p.stdout
        if rc != 0:
            return None
        procs = [l for l in out.strip().splitlines() if l.strip()]
        return len(procs) == 0
    except Exception:
        return None
