"""What the builder actually does: build a tree, replay cases, sanitize, time.

Trust role: this is the semi-trusted executor. It runs code that came in
with a submission -- a Makefile the code's own people wrote, and the
binaries that Makefile produces -- on a machine with a compiler and a
GPU. Nothing here decides whether a port is good; the gateway does that
from what these functions report. What these functions must get right is
the reporting: which flags reached the compiler, which files were
compiled, which executable was run, and which files it wrote. A wrong
answer to any of those makes a claim describe a build or a run that did
not happen.

The build contract is one sentence: the tree says how to build itself.
The builder writes the submitted tree to disk with its directories
intact, runs `make` on the makefile the code's manifest names, and hands
the Makefile the strategy's compiler as a logging shim. Nothing here
knows a source file name, a module order, or a program name -- those all
come from the tree and the manifest.
"""
import base64
import glob
import os
import re
import shutil
import subprocess
import time

from . import contract

HARNESS = "/opt/harness"  # baked, trusted: npy_io.f90, fc-shim
WORK_ROOT = "/work"  # one workspace per attempt, rebuilt from scratch each build

# The capture format on disk: one file per variable in the case directory,
# <variable>.npy going in and <variable>.out.npy coming out. Each file says
# for itself what type and shape it holds, so this service never has to be
# told a variable name or an element type.
INPUT_SUFFIX = ".npy"
OUTPUT_SUFFIX = ".out.npy"

# The shim's log, and how long a whole build may take. A build now runs a
# project's real makefile, which may configure as well as compile, so the
# ceiling is generous; a hung build still ends rather than holding the
# service forever.
LOG_NAME = "fc.jsonl"
BUILD_TIMEOUT_S = 1800
REPLAY_TIMEOUT_S = 120
SANITIZE_TIMEOUT_S = 600


def _workspace(attempt_id, work_root):
    return os.path.join(work_root, re.sub(r"[^A-Za-z0-9._-]", "_", attempt_id))


def _tree_dir(attempt_id, work_root):
    return os.path.join(_workspace(attempt_id, work_root), "tree")


def write_tree(tree_dir, tree) -> str:
    """Write the submitted files under `tree_dir`, directories and all.

    `tree` is [{"path": str, "b64": str}] -- the whole tracked tree, not a
    filtered source list, because a code's build reads namelists, include
    files, and data the harness has no way to recognize. Paths are
    relative to the tree root and may not climb out of it: a path that
    would write outside the workspace is refused by name rather than
    written somewhere surprising.
    """
    tree_dir = os.path.abspath(tree_dir)
    for entry in tree:
        path = entry["path"]
        if os.path.isabs(path) or not path or ".." in path.replace("\\", "/").split("/"):
            raise ValueError(f"tree path {path!r} does not stay inside the tree")
        destination = os.path.join(tree_dir, path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as out:
            out.write(base64.b64decode(entry["b64"]))
    return tree_dir


def _run(cmd, cwd=None, env=None, timeout=300):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _read_log(path: str) -> str:
    """The shim's log, or nothing at all if the Makefile never called it."""
    if not os.path.exists(path):
        return ""
    with open(path) as log:
        return log.read()


def _accel_lines(text: str) -> str:
    """The compiler's own account of what it offloaded, for a reader of the claim."""
    lines = [
        line for line in text.splitlines()
        if "Generating" in line or "Loop" in line or "GPU" in line
    ]
    return "\n".join(lines[:40])


def build(attempt_id, tree, makefile, targets, compiler, flags, link_flags, source_patterns,
          *, harness_dir=HARNESS, work_root=WORK_ROOT, timeout=BUILD_TIMEOUT_S) -> dict:
    """Build the submitted tree with its own makefile, and say what that did.

    `targets` is [{"role", "target", "executable"}] straight from the
    code's manifest: `make` is asked for each `target`, and each
    `executable` must exist in the tree root afterwards.

    The strategy's flags are put in the environment rather than on make's
    command line. A command-line assignment would override a Makefile
    that sets FFLAGS itself, which sounds safer and is worse: the flags
    would appear to have been used no matter what the Makefile does, and
    the shim log would have nothing to prove. In the environment, a
    Makefile that ignores FFLAGS wins -- and is then visible.
    """
    workspace = _workspace(attempt_id, work_root)
    shutil.rmtree(workspace, ignore_errors=True)
    tree_dir = write_tree(os.path.join(workspace, "tree"), tree)
    log_path = os.path.join(workspace, LOG_NAME)

    env = {
        **os.environ,
        "FC": os.path.join(harness_dir, "fc-shim"),
        "FC_REAL": compiler,
        "FC_LOG": log_path,
        "FFLAGS": " ".join(flags),
        "LDFLAGS": " ".join(link_flags),
        "HARNESS": harness_dir,
    }
    module_flag = contract.module_flag(compiler)
    if module_flag is not None:
        env["MODFLAG"] = module_flag

    command = ["make", "-f", makefile, *[t["target"] for t in targets]]
    try:
        rc, out, err = _run(command, cwd=tree_dir, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        rc, out, err = 1, "", f"the build did not finish within {timeout} seconds"
    output = out + err

    compiles = contract.compile_records(
        _read_log(log_path), tree_dir, flags, source_patterns, harness_dir=harness_dir,
    )
    built = {
        t["role"]: {
            "executable": t["executable"],
            "built": os.path.exists(os.path.join(tree_dir, t["executable"])),
        }
        for t in targets
    }
    result = {
        "stage": "build",
        "command": command,
        "targets": built,
        "compiles": compiles,
        "flags": list(flags),
        "link_flags": list(link_flags),
        "flags_reached_every_compile": contract.flags_reached_every_compile(compiles),
        "compiled_only_tree_source": contract.compiled_only_tree_source(compiles),
        "minfo_excerpt": _accel_lines(output),
    }

    if rc != 0:
        return {**result, "ok": False, "log_tail": output[-4000:]}

    missing = sorted(role for role, target in built.items() if not target["built"])
    if missing:
        # make said it succeeded and the executable is not there: almost
        # always a manifest naming a different file than the rule writes.
        named = ", ".join(f"{role} -> {built[role]['executable']}" for role in missing)
        return {
            **result, "ok": False, "missing_targets": missing,
            "log_tail": f"make succeeded but left no executable for: {named}\n{output[-3000:]}",
        }
    return {**result, "ok": True, "log_tail": output[-2000:]}


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


# One kernel launch as NVCOMPILER_ACC_NOTIFY=1 writes it. Both offload
# flavors print the same first four fields, differing only in what follows
# and in whether one or two spaces sit after "kernel":
#
#   launch CUDA kernel  file=... function=p line=5 device=0 threadid=1 num_gangs=...
#   launch CUDA kernel file=... function=q line=5 device=0 host-threadid=0 num_teams=...
#
# Requiring those four fields is what makes the count proof: a program can
# print the words "launch CUDA kernel" itself, but not the runtime's own
# file/function/line/device fields for a kernel it never launched.
LAUNCH_LINE = re.compile(r"^launch CUDA kernel\s+file=(\S+) function=(\S+) line=(\d+) device=(\d+)")


def kernel_launches(stderr, notify):
    """(how many kernels launched, [(file, function, line), ...]) from one run's stderr.

    Returns nothing counted for a strategy that asked for no notify
    output: without NVCOMPILER_ACC_NOTIFY set there are no lines to read,
    so any that appear were written by the program itself.
    """
    if notify not in ("acc", "omp"):
        return 0, []
    found = [
        (m.group(1), m.group(2), m.group(3))
        for m in (LAUNCH_LINE.match(line) for line in stderr.splitlines())
        if m
    ]
    return len(found), found


def _write_case(cdir, arrs):
    """One case directory holding the inputs the caller sent, and nothing else.

    The variable names come from the request; what each file holds comes
    from the file. The directory is rebuilt from scratch every time, so a
    replay never sees an output an earlier run left behind.
    """
    shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir, exist_ok=True)
    for variable, encoded in arrs.items():
        with open(os.path.join(cdir, f"{variable}{INPUT_SUFFIX}"), "wb") as f:
            f.write(base64.b64decode(encoded))
    return cdir


def _read_outputs(cdir):
    """Every output file the replay driver left in one case directory.

    The builder is not told which outputs to expect: it returns whatever
    the driver wrote, and the gateway and the oracle are the ones that
    know what the code declares. So a driver that wrote nothing produces
    an empty set here rather than an error about a name this file guessed.
    """
    outputs = {}
    for path in sorted(glob.glob(os.path.join(cdir, f"*{OUTPUT_SUFFIX}"))):
        variable = os.path.basename(path)[: -len(OUTPUT_SUFFIX)]
        with open(path, "rb") as f:
            outputs[variable] = base64.b64encode(f.read()).decode()
    return outputs


def _executable(attempt_id, executable, work_root):
    """The absolute path of an executable the manifest named, if it is there."""
    tree_dir = _tree_dir(attempt_id, work_root)
    path = os.path.join(tree_dir, executable)
    return tree_dir, path if os.path.exists(path) else None


# net_jail (unshare -n) is defense-in-depth. It needs CAP_SYS_ADMIN, which the
# builder container does not hold by default, and build_net is already
# internal: true (no internet, no route to the oracle) -- so we leave it off
# tonight and turn it on as later hardening once the container has the cap.
def run(attempt_id, executable, cases, notify=None, mandatory=False,
        *, work_root=WORK_ROOT, net_jail=False, timeout=REPLAY_TIMEOUT_S) -> dict:
    """Replay every case through the executable the code's manifest names.

    `cases` is {name: {variable: b64 npy}}. The driver is called as
    `<executable> <case_dir>` -- the one contract a replay driver has --
    and whatever `<variable>.out.npy` files it leaves come back.
    """
    tree_dir, replay = _executable(attempt_id, executable, work_root)
    if replay is None:
        return {
            "ok": False, "stage": "run",
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    env = _notify_env(os.environ, notify, mandatory)
    outputs = {}
    total_kernels = 0
    launched_at = set()
    log_tail = ""
    for name, arrs in cases.items():
        cdir = _write_case(os.path.join(_workspace(attempt_id, work_root), "cases", name), arrs)
        # jail the child from the network (defense in depth; build_net is already internal)
        prefix = ["unshare", "-n", "--"] if net_jail else []
        try:
            rc, out, err = _run(prefix + [replay, cdir], cwd=tree_dir, env=env, timeout=timeout)
        except Exception:
            rc, out, err = _run([replay, cdir], cwd=tree_dir, env=env, timeout=timeout)  # unshare not permitted
        if rc != 0:
            return {"ok": False, "stage": "run", "case": name, "log_tail": (out + err)[-2000:]}
        kernels, launches = kernel_launches(err, notify)
        total_kernels += kernels
        launched_at.update(launches)
        log_tail = err[-1500:]
        outputs[name] = _read_outputs(cdir)

    return {
        "ok": True, "stage": "run", "outputs": outputs,
        "kernels_launched": total_kernels,
        # Where the launches came from, one entry per distinct source line
        # across every case, so the claim says what ran and not only how
        # much of it ran.
        "launches": [list(where) for where in sorted(launched_at)],
        "log_tail": log_tail,
    }


def sanitize(attempt_id, executable, cases, tools, *, work_root=WORK_ROOT,
             timeout=SANITIZE_TIMEOUT_S) -> dict:
    """Run every tool over every case, against the manifest's replay executable.

    The caller chooses how many cases to send; whether that is one or all
    of them is the strategy's decision, not this file's. There is still
    one entry per tool in the response: the error counts are summed over
    the cases and a tool fails if it failed on any of them, so a caller
    that asks for more cases gets a stricter verdict, not more verdicts.
    """
    tree_dir, replay = _executable(attempt_id, executable, work_root)
    if replay is None:
        return {
            "ok": False, "stage": "sanitize", "per_tool": {},
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    per_tool = {}
    for tool in tools:
        errors = 0
        failed = False
        failing_log = ""
        last_log = ""
        unavailable = None
        for name, arrs in cases.items():
            cdir = _write_case(os.path.join(_workspace(attempt_id, work_root), "san", name), arrs)
            cmd = ["compute-sanitizer", "--tool", tool, "--error-exitcode", "1", replay, cdir]
            try:
                rc, out, err = _run(cmd, cwd=tree_dir, timeout=timeout)
            except FileNotFoundError:
                unavailable = "compute-sanitizer not found"
                break
            errors += len(re.findall(r"========= ERROR|Invalid|race", out + err))
            last_log = (out + err)[-1500:]
            if rc != 0 and not failed:
                failed = True
                failing_log = last_log
        if unavailable is not None:
            per_tool[tool] = {"ok": None, "error": unavailable}
        else:
            # The log of the first case that failed, so the reader sees the
            # failure rather than whatever the last case happened to print.
            per_tool[tool] = {"ok": not failed, "errors": errors, "log_tail": failing_log or last_log}
    return {"ok": all(t.get("ok") in (True, None) for t in per_tool.values()), "stage": "sanitize", "per_tool": per_tool}


def time_run(attempt_id, executable, args=(), env=None, outputs=(), repeats=5,
             budget_s=300, *, work_root=WORK_ROOT) -> dict:
    """Time the code's own program at the size its manifest declares.

    The arguments, the environment, the files the run is expected to
    write, and the per-run budget all come from the manifest, so the
    problem size is data rather than something compiled into a source
    file. The declared output files come back with the timings: they are
    what says the fast run was also a correct one.
    """
    tree_dir, program = _executable(attempt_id, executable, work_root)
    if program is None:
        return {
            "ok": False, "stage": "time",
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    # A file left by an earlier attempt would otherwise be collected as if
    # this run had written it.
    for relative in outputs:
        path = os.path.join(tree_dir, relative)
        if os.path.exists(path):
            os.remove(path)

    run_env = {**os.environ, **{str(k): str(v) for k, v in (env or {}).items()}}
    # honest timing wants exclusive GPU
    gpu_excl = _gpu_exclusive()
    runs = []
    last = ""
    for _ in range(repeats):
        t0 = time.monotonic()
        try:
            rc, out, err = _run([program, *args], cwd=tree_dir, env=run_env, timeout=budget_s)
        except subprocess.TimeoutExpired:
            return {
                "ok": False, "stage": "time", "runs_s": runs,
                "log_tail": f"a timing run exceeded the declared budget of {budget_s} seconds",
            }
        runs.append(time.monotonic() - t0)
        last = (out + err)[-1500:]
        if rc != 0:
            return {"ok": False, "stage": "time", "runs_s": runs, "log_tail": last}

    collected = {}
    for relative in outputs:
        path = os.path.join(tree_dir, relative)
        if not os.path.exists(path):
            return {
                "ok": False, "stage": "time", "runs_s": runs,
                "log_tail": f"the timing run wrote no '{relative}', which the manifest declares",
            }
        with open(path, "rb") as f:
            collected[relative] = base64.b64encode(f.read()).decode()

    return {
        "ok": True, "stage": "time", "runs_s": runs, "gpu_exclusive": gpu_excl,
        "outputs": collected, "stdout_tail": last,
    }


def _gpu_exclusive():
    try:
        rc, out, err = _run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"], timeout=15)
        procs = [l for l in out.strip().splitlines() if l.strip()]
        return len(procs) == 0
    except Exception:
        return None
