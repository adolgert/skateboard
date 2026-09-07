"""Case replay and capture stages."""
import os
import re
import shutil

from .case_io import CASE_FILE, _case_problem, _read_captured_case, _read_outputs, _write_case
from .contract import CaptureResponse, RunResponse
from .workspace import ExecutionFailed

REPLAY_TIMEOUT_S = 120
CAPTURE_TIMEOUT_S = 600

def _notify_env(base, notify, mandatory):
    env = dict(base)
    # NVCOMPILER_ACC_NOTIFY drives NVIDIA's own offload runtime and prints one
    # "launch CUDA kernel ..." line per launch. It covers BOTH -stdpar/OpenACC
    # and -mp=gpu OpenMP target regions, because nvfortran runs them on the same
    # runtime. LIBOMPTARGET_INFO is an LLVM/Clang offload variable: nvfortran
    # ignores it entirely and emits nothing, which made every omp_target attempt
    # report kernels_launched=0 and fail the device proof regardless of merit.
    if notify in ("acc", "omp"):
        env["NVCOMPILER_ACC_NOTIFY"] = "1"      # print each kernel launch to stderr
    if mandatory:
        env["OMP_TARGET_OFFLOAD"] = "MANDATORY"  # host fallback becomes a runtime error
    return env
def run(workspace, executable, cases, notify=None, mandatory=False,
        *, timeout=REPLAY_TIMEOUT_S) -> RunResponse:
    """Replay every case through the executable the code's manifest names.

    `cases` is {name: {variable: b64 npy}}. The driver is called as
    `<executable> <case_dir>` -- the one contract a replay driver has --
    and whatever `<variable>.out.npy` files it leaves come back.
    """
    problem = _case_problem(cases)
    if problem is not None:
        return RunResponse.failure(problem)
    replay, identity = workspace.executable(executable)
    if replay is None:
        return RunResponse.failure(
            f"the tree holds no executable '{executable}'; build it first"
        )

    env = _notify_env(os.environ, notify, mandatory)
    profiled = notify in ("acc", "omp")
    outputs = {}
    total_kernels = 0
    launched_at = set()
    log_tail = ""
    for name, arrs in cases.items():
        try:
            cdir = _write_case(workspace.path("cases", name), arrs)
        except ValueError as exc:
            return RunResponse.failure(f"case '{name}': {exc}", case=name)
        workspace.prepare_job_files()
        try:
            job = workspace.execute(
                [replay, cdir], cwd=workspace.tree_dir, env=env, timeout=timeout,
                mode="profiled" if profiled else "plain", gpu=True,
                what=f"the replay of case '{name}'",
            )
        except ExecutionFailed as exc:
            return RunResponse.failure(str(exc), case=name)
        profile = job.evidence if profiled else {
            "ok": True, "kernels_launched": 0, "kernel_names": [],
        }
        if job.returncode != 0:
            return RunResponse.failure((job.stdout + job.stderr)[-2000:], case=name)
        if not workspace.matches(replay, identity):
            return RunResponse.failure(
                "the executable changed while it was being measured", case=name,
            )
        if profiled and (not profile or not profile.get("ok")):
            return RunResponse.failure(
                (profile or {}).get("error", "protected nsys evidence was unavailable"),
                case=name,
            )
        total_kernels += int(profile.get("kernels_launched", 0))
        launched_at.update(("nsys", kernel, "0") for kernel in profile.get("kernel_names", []))
        log_tail = job.stderr[-1500:]
        try:
            outputs[name] = _read_outputs(cdir)
        except ValueError as exc:
            return RunResponse.failure(str(exc), case=name)

    return RunResponse(
        ok=True, outputs=outputs, kernels_launched=total_kernels,
        launches=[list(where) for where in sorted(launched_at)],
        profiler="nsys/CUPTI_ACTIVITY_KIND_KERNEL" if profiled else None,
        executable_identity=identity, log_tail=log_tail,
    )
def capture(workspace, executable, args=(), run_name="capture",
            *, timeout=CAPTURE_TIMEOUT_S) -> CaptureResponse:
    """Run the code's own capture program and return the dataset it wrote.

    The contract is one line: `<executable> <args...> <outdir>`, where the
    arguments are the dataset's own, from the manifest, and the output
    directory is this service's to name. It is made empty first, so what
    comes back is what this run wrote and not what an earlier one left.

    A case is a directory holding `case.json`; anything else the program
    writes beside them is ignored. A run that leaves none is not a
    crash -- the program ran and produced no dataset -- so it comes back
    as `ok: false` saying that, for the gateway to turn into a verdict.
    """
    program, identity = workspace.executable(executable)
    if program is None:
        return CaptureResponse.failure(
            f"the tree holds no executable '{executable}'; build it first"
        )

    safe_run = re.sub(r"[^A-Za-z0-9._-]", "_", run_name)
    outdir = workspace.path("captures", safe_run)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)

    workspace.prepare_job_files()
    try:
        job = workspace.execute(
            [program, *args, outdir], cwd=workspace.tree_dir, timeout=timeout,
            gpu=True, what="the capture run",
        )
    except ExecutionFailed as exc:
        return CaptureResponse.failure(str(exc))
    tail = (job.stdout + job.stderr)[-2000:]
    if job.returncode != 0:
        return CaptureResponse.failure(tail)
    if not workspace.matches(program, identity):
        return CaptureResponse.failure("the executable changed while it was being measured")

    cases = {}
    for name in sorted(os.listdir(outdir)):
        case_dir = os.path.join(outdir, name)
        if not os.path.isdir(case_dir) or not os.path.exists(os.path.join(case_dir, CASE_FILE)):
            continue
        try:
            cases[name] = _read_captured_case(case_dir)
        except (OSError, ValueError) as exc:
            return CaptureResponse.failure(f"case '{name}': {exc}\n{tail}")

    if not cases:
        return CaptureResponse.failure(
            f"the capture run wrote no case directory (a directory holding "
            f"{CASE_FILE}) into the output directory it was given\n{tail}"
        )
    return CaptureResponse(
        ok=True, cases=cases, stdout_tail=tail, executable_identity=identity,
    )
