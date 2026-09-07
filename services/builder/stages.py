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
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import contract, executor, mutate as mutants

HARNESS = "/opt/harness"  # baked, trusted: npy_io.f90, fc-shim
WORK_ROOT = "/work"  # one workspace per attempt, rebuilt from scratch each build

# Production leaves this unset and therefore always uses DockerJobExecutor.
# Unit tests replace it with executor.local_run explicitly; it is not selected
# by an environment switch and cannot become a deployment fallback.
_JOB_RUNNER = None
_DOCKER_EXECUTOR = None

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
# A capture program runs the code's real setup at whatever size the
# dataset's arguments ask for, which is longer than a replay and shorter
# than a build.
CAPTURE_TIMEOUT_S = 600
# A property run makes one process per drawn example, so its ceiling is a
# whole search rather than a single call. Long enough for a few thousand
# invocations of a region, short enough that a driver that hangs on some
# drawn input ends the run instead of the service.
PROPERTIES_TIMEOUT_S = 900
# What one mutant costs at most: its own build and its replay of every
# case together. A mutant is a single-token change to a tree that has
# already built, so a mutant that has not finished in this long is one
# whose fault is a hang rather than a wrong number.
MUTATE_TIMEOUT_S = 300
# And what a whole mutation run costs at most, however many mutants that
# is. Reaching it leaves the rest unscored and says so. Long enough for a
# kernel of a few hundred lines, short enough that the answer arrives
# while the session that asked for it is still waiting.
MUTATE_CEILING_S = 1500
# How the mutation pool starts its workers. None means whatever this
# Python defaults to. A worker gets everything it needs from its job
# payload, so any start method works; a test names one to prove it.
MUTATE_START_METHOD = None
# How many mutants are built at once when the caller does not say. Each
# worker is a compile and a run, so this is chosen against a machine the
# rest of the harness is also using rather than against the core count.
DEFAULT_MUTATE_JOBS = 4

# The interpreter a code's property module is run under: this service's
# own. It is the same one /healthz imports pytest and Hypothesis with, so
# what the gateway was told is installed is what the run gets.
PYTHON = sys.executable or "python3"

# What a case directory says it holds. The builder reads this file for the
# variable names and nothing else -- no name and no element type is
# written down in this service.
CASE_FILE = "case.json"
# And what a directory of cases says it holds. A property module reads a
# whole dataset rather than one case, so it needs the listing too.
CASES_FILE = "cases.json"


def _workspace(attempt_id, work_root):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", attempt_id)
    if safe != attempt_id or safe in ("", ".", "..", ".artifacts"):
        # ``@`` cannot occur on the unchanged path above.  Reserving it for
        # encoded names prevents an attacker from supplying the sanitized name
        # of another attempt directly and landing in the same workspace.
        digest = hashlib.sha256(str(attempt_id).encode()).hexdigest()
        safe = f"@{(safe or 'attempt')[:80]}-{digest}"
    return os.path.join(work_root, safe)


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


def _job_executor(work_root=WORK_ROOT):
    global _DOCKER_EXECUTOR
    if _JOB_RUNNER is not None:
        return None
    if work_root != WORK_ROOT:
        raise executor.IsolationUnavailable(
            "a non-production work root requires an explicitly injected test job runner"
        )
    if _DOCKER_EXECUTOR is None:
        _DOCKER_EXECUTOR = executor.DockerJobExecutor(work_root=work_root)
    return _DOCKER_EXECUTOR


def _run(cmd, cwd=None, env=None, timeout=300, *, work_root=WORK_ROOT, gpu=False):
    runner = _JOB_RUNNER
    result = (
        runner(cmd, cwd=cwd, env=env, timeout=timeout, profile_gpu=False, gpu=gpu)
        if runner is not None
        else _job_executor(work_root).run(cmd, cwd=cwd, env=env, timeout=timeout, gpu=gpu)
    )
    return result.returncode, result.stdout, result.stderr


def _run_audited(cmd, cwd=None, env=None, timeout=300, *, work_root=WORK_ROOT, gpu=False):
    runner = _JOB_RUNNER
    result = (
        runner(cmd, cwd=cwd, env=env, timeout=timeout, audit_exec=True, gpu=gpu)
        if runner is not None
        else _job_executor(work_root).run(
            cmd, cwd=cwd, env=env, timeout=timeout, audit_exec=True, gpu=gpu,
        )
    )
    return result.returncode, result.stdout, result.stderr, result.evidence


def _run_profiled(cmd, cwd=None, env=None, timeout=300, *, work_root=WORK_ROOT):
    runner = _JOB_RUNNER
    result = (
        runner(cmd, cwd=cwd, env=env, timeout=timeout, profile_gpu=True)
        if runner is not None
        else _job_executor(work_root).run(
            cmd, cwd=cwd, env=env, timeout=timeout, profile_gpu=True,
        )
    )
    return result.returncode, result.stdout, result.stderr, result.evidence


def isolation_status() -> dict:
    """Deployment readiness; absence of the boundary makes health fail closed."""
    try:
        if _JOB_RUNNER is not None:
            return {"ok": True, "backend": "injected-test-runner"}
        return _job_executor().probe()
    except Exception as exc:
        return {"ok": False, "backend": "docker", "error": str(exc)}


def _artifact_file(attempt_id, work_root):
    name = os.path.basename(_workspace(attempt_id, work_root)) + ".json"
    return os.path.join(work_root, ".artifacts", name)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_artifacts(attempt_id, targets, work_root):
    records = {}
    tree_dir = _tree_dir(attempt_id, work_root)
    for target in targets:
        path = _in_tree(tree_dir, target["executable"])
        if path is None or not os.path.isfile(path) or os.path.islink(path):
            continue
        records[target["executable"]] = {
            "sha256": _sha256_file(path), "size": os.path.getsize(path),
            "role": target["role"],
        }
    directory = os.path.dirname(_artifact_file(attempt_id, work_root))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    destination = _artifact_file(attempt_id, work_root)
    temporary = destination + ".new"
    execution = isolation_status() if _JOB_RUNNER is None else {
        "executor_identity": "explicit-local-test-runner", "image_id": None,
    }
    with open(temporary, "w", encoding="utf-8") as out:
        json.dump({
            "attempt_id": attempt_id, "executables": records,
            "executor_identity": execution.get("executor_identity"),
            "image_id": execution.get("image_id"),
        }, out, sort_keys=True)
    os.replace(temporary, destination)
    return records


def artifact_identities(attempt_id, *, work_root=WORK_ROOT) -> dict:
    try:
        with open(_artifact_file(attempt_id, work_root), encoding="utf-8") as source:
            record = json.load(source)
    except (OSError, ValueError):
        return {"ok": False, "attempt_id": attempt_id, "executables": {}}
    valid = {}
    tree_dir = _tree_dir(attempt_id, work_root)
    for relative, identity in record.get("executables", {}).items():
        path = _in_tree(tree_dir, relative)
        matches = bool(
            path and os.path.isfile(path) and not os.path.islink(path)
            and os.path.getsize(path) == identity.get("size")
            and _sha256_file(path) == identity.get("sha256")
        )
        valid[relative] = {**identity, "verified": matches}
    return {
        "ok": bool(valid) and all(item["verified"] for item in valid.values()),
        "attempt_id": attempt_id, "executables": valid,
        "executor_identity": record.get("executor_identity"),
        "image_id": record.get("image_id"),
    }


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


def build_env(compiler, flags, link_flags, log_path, harness_dir=HARNESS) -> dict:
    """The environment a submitted makefile is run in, wherever it is run.

    The compiler it is handed is the logging shim, not the strategy's
    compiler directly, so every invocation is recorded; the flags are in
    the environment rather than on make's command line, so a makefile
    that ignores them wins and is then visible in the log. The mutation
    stage builds its mutants with this same environment, because a mutant
    built some other way would say nothing about the build a port faces.
    """
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
    return env


def _prepare_job_files(workspace, *, include_tree=False):
    """Give the fixed unprivileged job uid ownership of this attempt only."""
    if _JOB_RUNNER is not None:
        return
    workspace = os.path.abspath(workspace)
    protected_parent = os.path.isdir(os.path.join(workspace, "tree"))
    for root, directories, files in os.walk(workspace):
        if not include_tree and os.path.basename(root) == "tree" and os.path.dirname(root) == workspace:
            directories[:] = []
            continue
        if not include_tree and root == workspace and "tree" in directories:
            directories.remove("tree")
        if protected_parent and root == workspace:
            os.chown(root, 0, 0, follow_symlinks=False)
            os.chmod(root, 0o555)
        else:
            os.chown(root, 65532, 65532, follow_symlinks=False)
        for name in directories:
            os.chown(os.path.join(root, name), 65532, 65532, follow_symlinks=False)
        for name in files:
            os.chown(os.path.join(root, name), 65532, 65532, follow_symlinks=False)


def _freeze_tree(tree_dir, identities):
    """After build, submitted jobs can read code and execute bound binaries only."""
    if _JOB_RUNNER is not None:
        return
    executables = {os.path.normpath(name) for name in identities}
    for root, directories, files in os.walk(tree_dir):
        os.chown(root, 0, 0, follow_symlinks=False)
        os.chmod(root, 0o555)
        for name in directories:
            path = os.path.join(root, name)
            os.chown(path, 0, 0, follow_symlinks=False)
            if not os.path.islink(path):
                os.chmod(path, 0o555)
        for name in files:
            path = os.path.join(root, name)
            relative = os.path.relpath(path, tree_dir)
            os.chown(path, 0, 0, follow_symlinks=False)
            if not os.path.islink(path):
                os.chmod(path, 0o555 if relative in executables else 0o444)


def _identity_matches(path, identity):
    return bool(
        identity and os.path.isfile(path) and not os.path.islink(path)
        and os.path.getsize(path) == identity.get("size")
        and _sha256_file(path) == identity.get("sha256")
    )


def _writable_copy(path):
    """Make a disposable copied tree usable as an application work directory."""
    if _JOB_RUNNER is not None:
        return
    for root, directories, files in os.walk(path):
        os.chmod(root, 0o755)
        for name in directories:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                os.chmod(child, 0o755)
        for name in files:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                mode = os.stat(child).st_mode
                os.chmod(child, 0o755 if mode & 0o111 else 0o644)


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

    makefile_path = _in_tree(tree_dir, makefile)
    if makefile_path is None or not os.path.isfile(makefile_path):
        return {
            "ok": False, "stage": "build", "targets": {}, "compiles": [],
            "log_tail": f"the makefile '{makefile}' is not a regular file inside the tree",
        }
    for target in targets:
        if _in_tree(tree_dir, target["executable"]) is None:
            return {
                "ok": False, "stage": "build", "targets": {}, "compiles": [],
                "log_tail": f"target executable '{target['executable']}' leaves the tree",
            }

    env = build_env(compiler, flags, link_flags, log_path, harness_dir)
    # The protected exec observer records the compiler itself.  Giving make
    # the real compiler also removes the writable fc.jsonl shim as a trust root.
    if _JOB_RUNNER is None:
        env["FC"] = compiler
    command = ["make", "-f", makefile, *[t["target"] for t in targets]]
    _prepare_job_files(workspace, include_tree=True)
    try:
        rc, out, err, audit = _run_audited(
            command, cwd=tree_dir, env=env, timeout=timeout, work_root=work_root,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "stage": "build", "targets": {}, "compiles": [],
            "log_tail": f"the build did not finish within {timeout} seconds",
        }
    output = out + err

    if _JOB_RUNNER is None:
        if not audit or audit.get("ok") is not True:
            return {
                "ok": False, "stage": "build", "targets": {}, "compiles": [],
                "log_tail": "protected compiler execution evidence was unavailable",
            }
        compiler_path = shutil.which(compiler)
        if compiler_path is None:
            return {
                "ok": False, "stage": "build", "targets": {}, "compiles": [],
                "log_tail": f"configured compiler '{compiler}' is not installed",
            }
        compiler_real = os.path.realpath(compiler_path)
        observed = [
            entry["argv"] for entry in audit.get("executions", [])
            if isinstance(entry, dict) and entry.get("argv")
            and os.path.realpath(entry.get("path", "")) == compiler_real
        ]
        if not observed:
            return {
                "ok": False, "stage": "build", "targets": {}, "compiles": [],
                "log_tail": f"protected exec tracing observed no invocation of '{compiler_real}'",
            }
        protected_log = "\n".join(json.dumps({"argv": argv[1:], "cwd": tree_dir}) for argv in observed)
        compiler_audit = {
            "protected": True,
            "collector": "strace/execve",
            "observed_executions": len(audit.get("executions", [])),
            "compiler_invocations": len(observed),
        }
    else:
        protected_log = _read_log(log_path)
        compiler_audit = {
            "protected": False,
            "collector": "explicit-local-test-runner/fc-shim",
        }
    compiles = contract.compile_records(
        protected_log, tree_dir, flags, source_patterns, harness_dir=harness_dir,
    )
    built = {}
    for target in targets:
        path = _in_tree(tree_dir, target["executable"])
        built[target["role"]] = {
            "executable": target["executable"],
            "built": bool(
                path and os.path.isfile(path) and not os.path.islink(path)
                and os.access(path, os.X_OK)
            ),
        }
    result = {
        "stage": "build",
        "command": command,
        "targets": built,
        "compiles": compiles,
        "compiler_audit": compiler_audit,
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
    identities = _write_artifacts(attempt_id, targets, work_root)
    _freeze_tree(tree_dir, identities)
    for target in built.values():
        target.update(identities[target["executable"]])
    artifact_record = artifact_identities(attempt_id, work_root=work_root)
    return {
        **result, "ok": True, "log_tail": output[-2000:],
        "executor_identity": artifact_record.get("executor_identity"),
        "image_id": artifact_record.get("image_id"),
    }


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
        if not _plain_name(variable):
            raise ValueError(f"case variable {variable!r} is not a plain name")
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
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("a replay output is not a regular file")
        variable = os.path.basename(path)[: -len(OUTPUT_SUFFIX)]
        with open(path, "rb") as f:
            outputs[variable] = base64.b64encode(f.read()).decode()
    return outputs


def _executable(attempt_id, executable, work_root):
    """A built executable only while its supervisor-held identity still matches."""
    tree_dir = _tree_dir(attempt_id, work_root)
    path = _in_tree(tree_dir, executable)
    if path is None or not os.path.isfile(path) or os.path.islink(path):
        return tree_dir, None, None
    # Explicit local test runners construct tiny fixture trees without going
    # through build().  Production always requires the protected sidecar.
    if _JOB_RUNNER is not None:
        identity = {"sha256": _sha256_file(path), "size": os.path.getsize(path)}
        return tree_dir, path, identity
    artifact_record = artifact_identities(attempt_id, work_root=work_root)
    identity = artifact_record.get("executables", {}).get(executable)
    if not identity or not identity.get("verified"):
        return tree_dir, None, None
    answer = {k: identity[k] for k in ("sha256", "size", "role") if k in identity}
    answer["executor_identity"] = artifact_record.get("executor_identity")
    return tree_dir, path, answer


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
    tree_dir, replay, identity = _executable(attempt_id, executable, work_root)
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
        if not _plain_name(name):
            return {
                "ok": False, "stage": "run", "case": str(name),
                "log_tail": f"case name {name!r} is not a plain name",
            }
        cdir = _write_case(os.path.join(_workspace(attempt_id, work_root), "cases", name), arrs)
        _prepare_job_files(_workspace(attempt_id, work_root))
        try:
            if notify in ("acc", "omp"):
                rc, out, err, profile = _run_profiled(
                    [replay, cdir], cwd=tree_dir, env=env, timeout=timeout,
                    work_root=work_root,
                )
            else:
                rc, out, err = _run(
                    [replay, cdir], cwd=tree_dir, env=env, timeout=timeout,
                    work_root=work_root, gpu=True,
                )
                profile = {"ok": True, "kernels_launched": 0, "kernel_names": []}
        except executor.IsolationUnavailable as exc:
            return {"ok": False, "stage": "run", "case": name, "log_tail": str(exc)}
        if rc != 0:
            return {"ok": False, "stage": "run", "case": name, "log_tail": (out + err)[-2000:]}
        if not _identity_matches(replay, identity):
            return {
                "ok": False, "stage": "run", "case": name,
                "log_tail": "the executable changed while it was being measured",
            }
        if notify in ("acc", "omp") and (not profile or not profile.get("ok")):
            return {
                "ok": False, "stage": "run", "case": name,
                "log_tail": (profile or {}).get("error", "protected nsys evidence was unavailable"),
            }
        kernels = int(profile.get("kernels_launched", 0))
        launches = [("nsys", name, "0") for name in profile.get("kernel_names", [])]
        total_kernels += kernels
        launched_at.update(launches)
        log_tail = err[-1500:]
        try:
            outputs[name] = _read_outputs(cdir)
        except ValueError as exc:
            return {"ok": False, "stage": "run", "case": name, "log_tail": str(exc)}

    return {
        "ok": True, "stage": "run", "outputs": outputs,
        "kernels_launched": total_kernels,
        # Where the launches came from, one entry per distinct source line
        # across every case, so the claim says what ran and not only how
        # much of it ran.
        "launches": [list(where) for where in sorted(launched_at)],
        "profiler": "nsys/CUPTI_ACTIVITY_KIND_KERNEL" if notify in ("acc", "omp") else None,
        "executable_identity": identity,
        "log_tail": log_tail,
    }


def _plain_name(name) -> bool:
    """Is this a variable name and not a way out of the case directory."""
    return (
        isinstance(name, str) and name not in ("", ".", "..")
        and "/" not in name and "\\" not in name
    )


def _read_captured_case(case_dir):
    """One captured case: the files `case.json` lists, base64 as they are on disk.

    A name the listing gives but the program never wrote is left out
    rather than invented, so the gateway sees a case that is missing a
    variable and can say which one. It is the gateway, holding the code's
    manifest, that knows what the case should have held.
    """
    listing = os.path.join(case_dir, CASE_FILE)
    if os.path.islink(listing) or not os.path.isfile(listing):
        raise ValueError(f"{CASE_FILE} is not a regular file")
    with open(listing) as f:
        listed = json.load(f)
    case = {}
    for section, suffix in (("inputs", INPUT_SUFFIX), ("outputs", OUTPUT_SUFFIX)):
        arrays = {}
        for name in listed.get(section, []):
            if not _plain_name(name):
                raise ValueError(f"{CASE_FILE} lists {name!r}, which is not a variable name")
            path = os.path.join(case_dir, f"{name}{suffix}")
            if os.path.islink(path):
                raise ValueError(f"captured array '{name}' is a symbolic link")
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    arrays[name] = base64.b64encode(f.read()).decode()
        case[section] = arrays
    return case


def capture(attempt_id, executable, args=(), run_name="capture", *, work_root=WORK_ROOT,
            timeout=CAPTURE_TIMEOUT_S) -> dict:
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
    tree_dir, program, identity = _executable(attempt_id, executable, work_root)
    if program is None:
        return {
            "ok": False, "stage": "capture", "cases": {},
            "stdout_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    safe_run = re.sub(r"[^A-Za-z0-9._-]", "_", run_name)
    outdir = os.path.join(_workspace(attempt_id, work_root), "captures", safe_run)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)

    try:
        _prepare_job_files(_workspace(attempt_id, work_root))
        rc, out, err = _run(
            [program, *args, outdir], cwd=tree_dir, timeout=timeout, work_root=work_root,
            gpu=True,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "stage": "capture", "cases": {},
            "stdout_tail": f"the capture run did not finish within {timeout} seconds",
        }
    tail = (out + err)[-2000:]
    if rc != 0:
        return {"ok": False, "stage": "capture", "cases": {}, "stdout_tail": tail}
    if not _identity_matches(program, identity):
        return {
            "ok": False, "stage": "capture", "cases": {},
            "stdout_tail": "the executable changed while it was being measured",
        }

    cases = {}
    for name in sorted(os.listdir(outdir)):
        case_dir = os.path.join(outdir, name)
        if not os.path.isdir(case_dir) or not os.path.exists(os.path.join(case_dir, CASE_FILE)):
            continue
        try:
            cases[name] = _read_captured_case(case_dir)
        except (OSError, ValueError) as exc:
            return {
                "ok": False, "stage": "capture", "cases": {},
                "stdout_tail": f"case '{name}': {exc}\n{tail}",
            }

    if not cases:
        return {
            "ok": False, "stage": "capture", "cases": {},
            "stdout_tail": f"the capture run wrote no case directory (a directory holding "
                           f"{CASE_FILE}) into the output directory it was given\n{tail}",
        }
    return {
        "ok": True, "stage": "capture", "cases": cases, "stdout_tail": tail,
        "executable_identity": identity,
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
    tree_dir, replay, identity = _executable(attempt_id, executable, work_root)
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
                _prepare_job_files(_workspace(attempt_id, work_root))
                rc, out, err = _run(
                    cmd, cwd=tree_dir, timeout=timeout, work_root=work_root, gpu=True,
                )
            except FileNotFoundError:
                unavailable = "compute-sanitizer not found"
                break
            errors += len(re.findall(r"========= ERROR|Invalid|race", out + err))
            if not _identity_matches(replay, identity):
                failed = True
                failing_log = "the executable changed while it was being measured"
                break
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
    return {
        "ok": bool(per_tool) and all(t.get("ok") is True for t in per_tool.values()),
        "stage": "sanitize", "per_tool": per_tool, "executable_identity": identity,
    }


def _write_dataset(directory, cases):
    """A directory of cases in the layout a property module reads.

    The same case directories `run` writes, plus the two listings that
    turn them into a dataset: `case.json` per case and `cases.json` for
    the set. No outputs are written -- what a property module is given is
    inputs, and what the region does with them is the thing under test.
    """
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    for name, arrs in cases.items():
        case_dir = _write_case(os.path.join(directory, name), arrs)
        with open(os.path.join(case_dir, CASE_FILE), "w") as f:
            json.dump({"inputs": sorted(arrs), "outputs": []}, f, indent=2)
    with open(os.path.join(directory, CASES_FILE), "w") as f:
        json.dump({"cases": sorted(cases)}, f, indent=2)
    return directory


# How pytest's own summary line spells what happened. The counts are read
# from it rather than from an exit code alone, so a claim can say how much
# ran and not only whether all of it passed.
COUNT_PATTERN = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b"
)


def pytest_counts(text) -> dict:
    """{passed, failed, errors} as pytest's summary reported them.

    The last figure for each word wins: pytest writes its summary at the
    end, and a failing test's own captured output can hold anything.
    """
    counts = {
        "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
        "deselected": 0, "xfailed": 0, "xpassed": 0,
    }
    for match in COUNT_PATTERN.finditer(text):
        word = match.group(2)
        counts["errors" if word.startswith("error") else word] = int(match.group(1))
    counts["collected"] = sum(
        counts[name] for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
    )
    counts["executed"] = sum(
        counts[name] for name in ("passed", "failed", "errors", "xfailed", "xpassed")
    )
    return counts


def _in_tree(tree_dir, relative):
    """The absolute path of a file the manifest named, or None if it left the tree.

    A properties module is a path out of the code's own manifest, so it
    gets the same treatment as a submitted tree path: one that climbs out
    of the tree, or is absolute, names a file this service will not run.
    """
    if not isinstance(relative, (str, os.PathLike)) or os.path.isabs(relative):
        return None
    tree_dir = os.path.abspath(tree_dir)
    path = os.path.normpath(os.path.join(tree_dir, relative))
    if path != tree_dir and not path.startswith(tree_dir + os.sep):
        return None
    real_root = os.path.realpath(tree_dir)
    real_path = os.path.realpath(path)
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        return None
    return path


def properties(attempt_id, executable, module, cases, seed, max_examples,
               *, work_root=WORK_ROOT, harness_dir=HARNESS,
               timeout=PROPERTIES_TIMEOUT_S) -> dict:
    """Run the code's own module of invariants against its replay binary.

    The module is a pytest file inside the tree, named by the code's
    manifest. It is run with the baked property library on PYTHONPATH and
    told, through the environment, which executable to invoke, which cases
    to draw from, where to write them, which seed to use, and how many
    examples to draw -- so the module itself names none of those.

    The seed and the example count come back with the counts pytest
    reported, because a property run is only repeatable if the claim says
    what it was: the same seed searches the same way, and a different one
    is a different search rather than a repeat.
    """
    tree_dir, replay, identity = _executable(attempt_id, executable, work_root)
    if replay is None:
        return {
            "ok": False, "stage": "properties", "seed": seed, "max_examples": max_examples,
            "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "deselected": 0, "xfailed": 0, "xpassed": 0,
            "collected": 0, "executed": 0,
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    module_path = _in_tree(tree_dir, module)
    if module_path is None or not os.path.isfile(module_path):
        where = "does not stay inside the tree" if module_path is None else "is not in the tree"
        return {
            "ok": False, "stage": "properties", "seed": seed, "max_examples": max_examples,
            "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "deselected": 0, "xfailed": 0, "xpassed": 0,
            "collected": 0, "executed": 0,
            "log_tail": f"the properties module '{module}' {where}",
        }

    workspace = _workspace(attempt_id, work_root)
    cases_dir = _write_dataset(os.path.join(workspace, "property_cases"), cases)
    scratch = os.path.join(workspace, "property_scratch")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(harness_dir), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
        ),
        "HARNESS_REPLAY": replay,
        "HARNESS_CASES": cases_dir,
        "HARNESS_SCRATCH": scratch,
        "HARNESS_SEED": str(seed),
        "HARNESS_MAX_EXAMPLES": str(max_examples),
    }
    # -p no:cacheprovider: the tree is a submission, not a checkout, and a
    # .pytest_cache written into it would be a file nobody sent.
    command = [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", module_path]
    _prepare_job_files(workspace)
    try:
        if _JOB_RUNNER is None:
            rc, out, err, audit = _run_audited(
                command, cwd=tree_dir, env=env, timeout=timeout,
                work_root=work_root, gpu=True,
            )
        else:
            rc, out, err = _run(
                command, cwd=tree_dir, env=env, timeout=timeout,
                work_root=work_root, gpu=True,
            )
            audit = None
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "stage": "properties", "seed": seed, "max_examples": max_examples,
            "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "deselected": 0, "xfailed": 0, "xpassed": 0,
            "collected": 0, "executed": 0,
            "log_tail": f"the property run did not finish within {timeout} seconds",
        }

    output = out + err
    replays_observed = None
    if _JOB_RUNNER is None:
        if not audit or audit.get("ok") is not True:
            return {
                "ok": False, "stage": "properties", "seed": seed,
                "max_examples": max_examples, **pytest_counts(output),
                "log_tail": "protected replay execution evidence was unavailable",
            }
        replay_job_path = "/job/tree/" + os.path.relpath(replay, tree_dir).replace(os.sep, "/")
        replays_observed = sum(
            1 for entry in audit.get("executions", [])
            if isinstance(entry, dict) and entry.get("path") == replay_job_path
        )
        if replays_observed == 0:
            return {
                "ok": False, "stage": "properties", "seed": seed,
                "max_examples": max_examples, **pytest_counts(output),
                "replays_observed": 0, "executable_identity": identity,
                "log_tail": "protected exec tracing observed no replay invocation",
            }
    if not _identity_matches(replay, identity):
        return {
            "ok": False, "stage": "properties", "seed": seed,
            "max_examples": max_examples, **pytest_counts(output),
            "executable_identity": identity,
            "log_tail": "the executable changed while it was being measured",
        }
    return {
        "ok": rc == 0, "stage": "properties", "seed": seed, "max_examples": max_examples,
        **pytest_counts(output),
        "replays_observed": replays_observed,
        "counts_source": "pytest summary emitted by the submitted property process",
        "executable_identity": identity,
        # Long enough to hold Hypothesis's minimized falsifying example,
        # which is the whole value of a failed property run.
        "log_tail": output[-4000:],
    }


def _output_arrays(case_dir) -> dict:
    """Every output file one replay left in a case directory, as arrays.

    The files say for themselves what they hold, so this reads them the
    way the oracle reads a capture: nothing here is told a variable name
    or an element type.
    """
    arrays = {}
    for path in sorted(glob.glob(os.path.join(case_dir, f"*{OUTPUT_SUFFIX}"))):
        variable = os.path.basename(path)[: -len(OUTPUT_SUFFIX)]
        arrays[variable] = np.load(path, allow_pickle=False)
    return arrays


def _write_mutated(mutant_dir, mutant) -> None:
    """The mutant's own copy of the file, with its one line changed."""
    path = os.path.join(mutant_dir, mutant.file)
    with open(path) as source:
        lines = source.read().split("\n")
    lines[mutant.line - 1] = mutant.mutated
    with open(path, "w") as out:
        out.write("\n".join(lines))


def _remaining(deadline) -> float:
    """What is left of one mutant's budget, never zero or negative."""
    return max(1.0, deadline - time.monotonic())


def score_mutant(job) -> dict:
    """Build one mutant, replay every case through it, and say what happened.

    Runs in a worker process, so everything it needs is in `job` and
    everything it answers with is in the returned row -- including which
    job runner to run its commands with, since a worker that was started
    rather than forked inherits nothing from this module. The mutant's
    directory is a copy of the tree that already built, so `make` rebuilds
    only what the changed file forces -- and it is removed again unless
    the verdict is one a person has to read the source of.
    """
    global _JOB_RUNNER
    _JOB_RUNNER = job["runner"]
    mutant = job["mutant"]
    mutant_dir = job["mutant_dir"]
    deadline = time.monotonic() + job["timeout"]
    try:
        shutil.rmtree(mutant_dir, ignore_errors=True)
        shutil.copytree(job["tree_dir"], mutant_dir, symlinks=True)
        _write_mutated(mutant_dir, mutant)
        _prepare_job_files(os.path.dirname(os.path.dirname(mutant_dir)))

        command = ["make", "-f", job["makefile"], job["target"]]
        try:
            rc, out, err = _run(
                command, cwd=mutant_dir, env=job["env"], timeout=_remaining(deadline),
                work_root=job["work_root"],
            )
        except subprocess.TimeoutExpired:
            mutant.status = mutants.BUILD_FAIL
            mutant.note = f"the mutant's build did not finish within {job['timeout']} seconds"
            return mutant.as_result()
        replay = os.path.join(mutant_dir, job["executable"])
        if rc != 0 or not os.path.exists(replay):
            mutant.status = mutants.BUILD_FAIL
            mutant.note = _last_line(out + err) or "make left no executable"
            return mutant.as_result()

        for name in job["cases"]:
            case_dir = os.path.join(mutant_dir, "cases", name)
            shutil.rmtree(case_dir, ignore_errors=True)
            shutil.copytree(os.path.join(job["inputs_root"], name), case_dir)
            try:
                rc, out, err = _run(
                    [replay, case_dir], cwd=mutant_dir, timeout=_remaining(deadline),
                    work_root=job["work_root"], gpu=True,
                )
            except subprocess.TimeoutExpired:
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = f"case '{name}': the replay did not finish in time"
                return mutant.as_result()
            if rc != 0:
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = f"case '{name}': exit {rc}: {_last_line(out + err)}"
                return mutant.as_result()

            try:
                expected = _output_arrays(os.path.join(job["refs_root"], name))
                got = _output_arrays(case_dir)
            except ValueError as exc:
                mutant.status = mutants.KILLED
                mutant.note = f"case '{name}': the replay wrote a file that is not an array ({exc})"
                return mutant.as_result()
            status, note = mutants.classify(expected, got, job["bands"])
            if status != mutants.EQUIVALENT:
                mutant.status, mutant.note = status, f"case '{name}': {note}"
                return mutant.as_result()

        mutant.status, mutant.note = mutants.EQUIVALENT, "no output changed in any case"
        return mutant.as_result()
    finally:
        if mutant.status not in mutants.KEEP_DIRECTORY:
            shutil.rmtree(mutant_dir, ignore_errors=True)


def _last_line(text) -> str:
    lines = [line for line in (text or "").strip().split("\n") if line.strip()]
    return lines[-1][:200] if lines else ""


def _refused(reason: str) -> dict:
    return {
        "ok": False, "stage": "mutate", "generated": 0, "scored": 0,
        "results": [], "counts": {}, "kept_dirs": [], "log_tail": reason,
    }


def _mutation_corpus(workspace, cases) -> tuple:
    """The cases laid out on disk once: inputs to replay, outputs to score against.

    Every mutant gets its own copy of the inputs, so the directory written
    here is read and never run in. The captured outputs are written beside
    them as the files they already are -- each says for itself what it
    holds -- and are what every mutant is compared with.
    """
    # Everything an earlier mutation run of this attempt left, including
    # the directories it kept for a reader: they belong to a run whose
    # answer has already been read, and keeping them would make it look
    # as though this run had produced them.
    shutil.rmtree(os.path.join(workspace, "mutants"), ignore_errors=True)
    inputs_root = os.path.join(workspace, "mutants", ".inputs")
    refs_root = os.path.join(workspace, "mutants", ".refs")
    for name, case in cases.items():
        _write_case(os.path.join(inputs_root, name), case.get("inputs", {}))
        reference = os.path.join(refs_root, name)
        os.makedirs(reference, exist_ok=True)
        for variable, encoded in case.get("outputs", {}).items():
            with open(os.path.join(reference, f"{variable}{OUTPUT_SUFFIX}"), "wb") as out:
                out.write(base64.b64decode(encoded))
    return inputs_root, refs_root


def mutate(attempt_id, makefile, replay_target, files, cases, bands, compiler, flags,
           link_flags, source_patterns, *, jobs=None, limit=None, work_root=WORK_ROOT,
           harness_dir=HARNESS, timeout=MUTATE_TIMEOUT_S, ceiling=MUTATE_CEILING_S) -> dict:
    """Score every mutant of the region's own files against the captured answers.

    This is the harness asking about itself: if a port of this region were
    wrong, would the gate notice? Each mutant is one changed token in one
    of the files the manifest says implement the region. It is built with
    the same makefile, compiler, flags and shim as the tree it came from,
    replayed on every case it is given, and compared with the captured
    outputs by the same comparator and the same bands a port is judged by
    -- so what comes back is a property of this gate as configured, not of
    a model of it.

    `replay_target` is the manifest's replay target, {"target",
    "executable"}: what `make` is asked for, and what it must leave behind.
    `cases` is {name: {"inputs": {variable: b64 npy}, "outputs": {...}}},
    the visible capture set. `bands` is the tolerance policy's band per
    output variable.

    There is no coverage prepass. gcov belongs to one compiler and the
    compiler here is whichever the strategy names, so a mutant on a line
    the cases never execute is built, run, and comes back as a survivor
    like any other. Reading the survivors is the point: some are
    equivalent code, and some are region the captured inputs never reach.

    Returns {ok, generated, scored, results, counts, kept_dirs}. Each
    result is one mutant and its verdict; the directories of the two
    verdicts a person has to read the source of are kept and named.
    """
    workspace = _workspace(attempt_id, work_root)
    tree_dir = _tree_dir(attempt_id, work_root)
    if not os.path.isdir(tree_dir):
        return _refused(
            f"there is no built tree for attempt '{attempt_id}'; build it before mutating it"
        )

    generated = []
    for relative in files:
        path = _in_tree(tree_dir, relative)
        if path is None or not os.path.isfile(path):
            where = "does not stay inside the tree" if path is None else "is not in the tree"
            return _refused(f"the region file '{relative}' {where}")
        if not contract.is_tree_source(relative, source_patterns):
            return _refused(
                f"the region file '{relative}' is not one this code calls its own source, "
                f"so mutating it would say nothing about a port of it"
            )
        with open(path, errors="replace") as source:
            generated.extend(mutants.generate(source.read(), relative))

    todo = generated[: int(limit)] if limit else list(generated)
    if not todo:
        return {
            "ok": True, "stage": "mutate", "generated": len(generated), "scored": 0,
            "results": [], "counts": {}, "kept_dirs": [],
            "log_tail": "no mutant was generated from the region's files",
        }

    inputs_root, refs_root = _mutation_corpus(workspace, cases)
    env = build_env(
        compiler, flags, link_flags, os.path.join(workspace, "mutants", ".fc.jsonl"), harness_dir,
    )
    if _JOB_RUNNER is None:
        env["FC"] = compiler
    payloads = [
        {
            "mutant": mutant,
            "runner": _JOB_RUNNER,
            "tree_dir": tree_dir,
            "mutant_dir": os.path.join(workspace, "mutants", mutant.mid),
            "makefile": makefile,
            "target": replay_target["target"],
            "executable": replay_target["executable"],
            "env": env,
            "inputs_root": inputs_root,
            "refs_root": refs_root,
            "cases": sorted(cases),
            "bands": bands,
            "timeout": timeout,
            "work_root": work_root,
        }
        for mutant in todo
    ]

    workers = int(jobs) if jobs else min(DEFAULT_MUTATE_JOBS, os.cpu_count() or 1)
    results = _score_all(payloads, workers, ceiling)
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "ok": True, "stage": "mutate", "generated": len(generated), "scored": len(results),
        "results": results, "counts": counts,
        "kept_dirs": [
            os.path.join(workspace, "mutants", row["id"]) for row in results
            if row["status"] in mutants.KEEP_DIRECTORY
        ],
    }



def _score_all(payloads, workers: int, ceiling) -> list:
    """Every mutant scored, in a pool of workers, inside one overall ceiling.

    Results come back in whatever order the workers finish; they are put
    back into the order the mutants were generated, so a reader of the
    claim walks the file from top to bottom. Anything the ceiling cut off
    is in that list too, saying so, rather than quietly absent.
    """
    by_id = {payload["mutant"].mid: payload["mutant"] for payload in payloads}
    scored = {}
    context = multiprocessing.get_context(MUTATE_START_METHOD) if MUTATE_START_METHOD else None
    pool = ProcessPoolExecutor(max_workers=max(1, workers), mp_context=context)
    deadline = time.monotonic() + ceiling
    try:
        futures = {pool.submit(score_mutant, payload): payload for payload in payloads}
        for future in list(futures):
            try:
                row = future.result(timeout=_remaining(deadline))
            except TimeoutError:
                break
            except Exception as exc:  # a worker that died is not a scored mutant
                mutant = futures[future]["mutant"]
                mutant.status, mutant.note = mutants.RUNTIME_FAIL, f"the scoring run failed: {exc}"
                row = mutant.as_result()
            scored[row["id"]] = row
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    rows = []
    for mid, mutant in by_id.items():
        if mid in scored:
            rows.append(scored[mid])
            continue
        mutant.status = mutants.SKIPPED
        mutant.note = "the mutation run reached its overall ceiling before this mutant"
        rows.append(mutant.as_result())
    return rows


def time_run(attempt_id, executable, args=(), env=None, outputs=(), repeats=5,
             budget_s=300, expected_outputs=None, *, work_root=WORK_ROOT) -> dict:
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
        return {
            "ok": False, "stage": "time", "runs_s": [], "outputs": [],
            "log_tail": "timing repeats must be a positive integer",
        }
    tree_dir, program, identity = _executable(attempt_id, executable, work_root)
    if program is None:
        return {
            "ok": False, "stage": "time",
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    run_env = {**os.environ, **{str(k): str(v) for k, v in (env or {}).items()}}
    if expected_outputs is not None and set(expected_outputs) != set(outputs):
        return {
            "ok": False, "stage": "time", "runs_s": [], "outputs": [],
            "log_tail": "expected timing outputs do not exactly match the declared outputs",
            "executable_identity": identity,
        }
    # honest timing wants exclusive GPU
    gpu_excl = _gpu_exclusive()
    runs = []
    collected = []
    last = ""
    for repetition in range(repeats):
        # Production executes the immutable binary from the frozen tree but
        # gives the application a fresh writable copy as its working directory.
        # This supports ordinary relative input/config files and arbitrary
        # scratch output without granting write access to the measured binary.
        if _JOB_RUNNER is None:
            run_dir = os.path.join(
                _workspace(attempt_id, work_root), "timing", f"run-{repetition + 1:04d}",
            )
            shutil.rmtree(run_dir, ignore_errors=True)
            shutil.copytree(tree_dir, run_dir, symlinks=True)
            _prepare_job_files(_workspace(attempt_id, work_root))
            _writable_copy(run_dir)
        else:
            run_dir = tree_dir
        # A file left by an earlier run, or by an earlier attempt, would
        # otherwise be collected as if this run had written it.
        for relative in outputs:
            path = _in_tree(run_dir, relative)
            if path is None:
                return {
                    "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                    "log_tail": f"declared timing output '{relative}' leaves the tree",
                }
            if run_dir == tree_dir and (
                os.path.samefile(path, program) if os.path.exists(path) else path == program
            ):
                return {
                    "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                    "log_tail": f"declared timing output '{relative}' is the measured executable",
                }
            if os.path.lexists(path):
                os.unlink(path)

        t0 = time.monotonic()
        try:
            rc, out, err = _run(
                [program, *args], cwd=run_dir, env=run_env, timeout=budget_s,
                # Relative application paths resolve in the disposable copy;
                # the executable path still names the frozen original.
                work_root=work_root, gpu=True,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                "log_tail": f"a timing run exceeded the declared budget of {budget_s} seconds",
            }
        runs.append(time.monotonic() - t0)
        last = (out + err)[-1500:]
        if rc != 0:
            return {
                "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                "log_tail": last,
            }
        if not _identity_matches(program, identity):
            return {
                "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                "log_tail": "the executable changed while it was being measured",
                "executable_identity": identity,
            }

        this_run = {}
        for relative in outputs:
            path = _in_tree(run_dir, relative)
            if path is None or os.path.islink(path) or not os.path.isfile(path):
                return {
                    "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                    "log_tail": f"run {len(runs)} wrote no '{relative}', which the "
                                f"manifest declares",
                }
            with open(path, "rb") as f:
                written = f.read()
            this_run[relative] = base64.b64encode(written).decode()
            if expected_outputs is not None and this_run[relative] != expected_outputs.get(relative):
                return {
                    "ok": False, "stage": "time", "runs_s": runs,
                    "outputs": [*collected, this_run],
                    "log_tail": f"run {len(runs)} wrote unexpected bytes to '{relative}'",
                    "executable_identity": identity,
                }
        collected.append(this_run)

    return {
        "ok": True, "stage": "time", "runs_s": runs, "gpu_exclusive": gpu_excl,
        "outputs": collected, "stdout_tail": last, "executable_identity": identity,
    }


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
