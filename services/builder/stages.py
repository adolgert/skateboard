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

Every stage is handed a workspace rather than an attempt id: where the
files are, who owns them while a job runs, what proves an executable is
the one that was built, and how a submitted command is run all live
there, so a stage is left with only its own question to answer.
"""
import base64
import glob
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from . import compile_log, mutate as mutants
from .contract import (
    BuildResponse, CaptureResponse, MutateResponse, PropertiesResponse,
    RunResponse, SanitizeResponse, TimeResponse,
)
from .workspace import WORK_ROOT, DisposableJobs, ExecutionFailed, Workspace, path_inside

HARNESS = "/opt/harness"  # baked, trusted: npy_io.f90, fc-shim

# How a submitted command is run when a caller does not say. None means the
# production policy: a fresh disposable container per command, the
# supervisor owning the files, and protected evidence of what executed.
# Unit tests replace it with the in-process policy explicitly; it is not
# selected by an environment switch and cannot become a deployment fallback.
POLICY = None

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


# The deployed policy, kept for the life of the process so the immutable
# job image is resolved once rather than once per request.
_PRODUCTION = None


def _production_policy(work_root=WORK_ROOT) -> DisposableJobs:
    global _PRODUCTION
    if _PRODUCTION is None or _PRODUCTION.work_root != os.path.abspath(str(work_root)):
        _PRODUCTION = DisposableJobs(work_root)
    return _PRODUCTION


def workspace_for(attempt_id, *, work_root=WORK_ROOT, policy=None) -> Workspace:
    """The one workspace an attempt owns, under the policy this service runs."""
    return Workspace(work_root, attempt_id, policy or POLICY or _production_policy(work_root))


def isolation_status() -> dict:
    """Deployment readiness; absence of the boundary makes health fail closed."""
    return (POLICY or _production_policy()).status()


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
    module_flag = compile_log.module_flag(compiler)
    if module_flag is not None:
        env["MODFLAG"] = module_flag
    return env


@dataclass
class Made:
    """One `make` invocation and what the compiler was observed to do in it."""

    command: list
    returncode: int
    output: str
    compiles: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)
    # Evidence that was required and was not there. A build whose account
    # of itself is missing is a failed build, not a quiet success.
    problem: str | None = None


def _observed_compiler_log(evidence, compiler, cwd):
    """The protected observer's account of the compile, in the shim's own format.

    Reading execve records rather than the shim's log removes the
    writable log file as a trust root: what is reported is what the
    kernel saw the configured compiler run with.
    """
    if not evidence or evidence.get("ok") is not True:
        return "", {}, "protected compiler execution evidence was unavailable"
    compiler_path = shutil.which(compiler)
    if compiler_path is None:
        return "", {}, f"configured compiler '{compiler}' is not installed"
    compiler_real = os.path.realpath(compiler_path)
    observed = [
        entry["argv"] for entry in evidence.get("executions", [])
        if isinstance(entry, dict) and entry.get("argv")
        and os.path.realpath(entry.get("path", "")) == compiler_real
    ]
    if not observed:
        return "", {}, f"protected exec tracing observed no invocation of '{compiler_real}'"
    log = "\n".join(json.dumps({"argv": argv[1:], "cwd": cwd}) for argv in observed)
    return log, {
        "protected": True,
        "collector": "strace/execve",
        "observed_executions": len(evidence.get("executions", [])),
        "compiler_invocations": len(observed),
    }, None


def _make(workspace, *, cwd, makefile, targets, env, timeout, compiler, flags,
          source_patterns, harness_dir, log_path, what) -> Made:
    """Run a code's own makefile, and read back what the compiler was asked to do.

    The one place a submitted build is invoked. The tree's build and each
    mutant's build come through here together, so a mutant is built,
    watched and read the same way the tree was -- a mutant built some
    other way would say nothing about the build a port faces.
    """
    command = ["make", "-f", makefile, *targets]
    job = workspace.execute(
        command, cwd=cwd, env=env, timeout=timeout, mode="audited", what=what,
    )
    if workspace.policy.audited:
        log, audit, problem = _observed_compiler_log(job.evidence, compiler, cwd)
    else:
        log, problem = _read_log(log_path), None
        audit = {"protected": False, "collector": "explicit-local-test-runner/fc-shim"}
    made = Made(command, job.returncode, job.stdout + job.stderr, audit=audit, problem=problem)
    if problem is None:
        made.compiles = compile_log.compile_records(
            log, cwd, flags, source_patterns, harness_dir=harness_dir,
        )
    return made


def build(workspace, tree, makefile, targets, compiler, flags, link_flags, source_patterns,
          *, harness_dir=HARNESS, timeout=BUILD_TIMEOUT_S) -> BuildResponse:
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
    workspace.reset()
    tree_dir = workspace.write_tree(tree)
    log_path = workspace.path(LOG_NAME)

    makefile_path = workspace.in_tree(makefile)
    if makefile_path is None or not os.path.isfile(makefile_path):
        return BuildResponse.failure(
            f"the makefile '{makefile}' is not a regular file inside the tree"
        )
    for target in targets:
        if workspace.in_tree(target["executable"]) is None:
            return BuildResponse.failure(
                f"target executable '{target['executable']}' leaves the tree"
            )

    env = build_env(compiler, flags, link_flags, log_path, harness_dir)
    # The protected exec observer records the compiler itself.  Giving make
    # the real compiler also removes the writable fc.jsonl shim as a trust root.
    if workspace.policy.audited:
        env["FC"] = compiler
    workspace.prepare_job_files(include_tree=True)
    try:
        made = _make(
            workspace, cwd=tree_dir, makefile=makefile,
            targets=[t["target"] for t in targets], env=env, timeout=timeout,
            compiler=compiler, flags=flags, source_patterns=source_patterns,
            harness_dir=harness_dir, log_path=log_path, what="the build",
        )
    except ExecutionFailed as exc:
        return BuildResponse.failure(str(exc))
    if made.problem is not None:
        return BuildResponse.failure(made.problem)

    built = {}
    for target in targets:
        path = workspace.in_tree(target["executable"])
        built[target["role"]] = {
            "executable": target["executable"],
            "built": bool(
                path and os.path.isfile(path) and not os.path.islink(path)
                and os.access(path, os.X_OK)
            ),
        }
    result = BuildResponse(
        command=made.command,
        targets=built,
        compiles=made.compiles,
        compiler_audit=made.audit,
        flags=list(flags),
        link_flags=list(link_flags),
        flags_reached_every_compile=compile_log.flags_reached_every_compile(made.compiles),
        compiled_only_tree_source=compile_log.compiled_only_tree_source(made.compiles),
        minfo_excerpt=_accel_lines(made.output),
    )

    if made.returncode != 0:
        result.log_tail = made.output[-4000:]
        return result

    missing = sorted(role for role, target in built.items() if not target["built"])
    if missing:
        # make said it succeeded and the executable is not there: almost
        # always a manifest naming a different file than the rule writes.
        named = ", ".join(f"{role} -> {built[role]['executable']}" for role in missing)
        result.missing_targets = missing
        result.log_tail = (
            f"make succeeded but left no executable for: {named}\n{made.output[-3000:]}"
        )
        return result

    identities = workspace.write_artifacts(targets)
    workspace.freeze_tree(identities)
    for target in built.values():
        target.update(identities[target["executable"]])
    record = workspace.artifact_identities()
    result.ok = True
    result.log_tail = made.output[-2000:]
    result.executor_identity = record.executor_identity
    result.image_id = record.image_id
    return result


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


# net_jail (unshare -n) is defense-in-depth. It needs CAP_SYS_ADMIN, which the
# builder container does not hold by default, and build_net is already
# internal: true (no internet, no route to the oracle) -- so we leave it off
# tonight and turn it on as later hardening once the container has the cap.
def run(workspace, executable, cases, notify=None, mandatory=False,
        *, timeout=REPLAY_TIMEOUT_S) -> RunResponse:
    """Replay every case through the executable the code's manifest names.

    `cases` is {name: {variable: b64 npy}}. The driver is called as
    `<executable> <case_dir>` -- the one contract a replay driver has --
    and whatever `<variable>.out.npy` files it leaves come back.
    """
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
        if not _plain_name(name):
            return RunResponse.failure(
                f"case name {name!r} is not a plain name", case=str(name),
            )
        cdir = _write_case(workspace.path("cases", name), arrs)
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


def sanitize(workspace, executable, cases, tools, *, timeout=SANITIZE_TIMEOUT_S) -> SanitizeResponse:
    """Run every tool over every case, against the manifest's replay executable.

    The caller chooses how many cases to send; whether that is one or all
    of them is the strategy's decision, not this file's. There is still
    one entry per tool in the response: the error counts are summed over
    the cases and a tool fails if it failed on any of them, so a caller
    that asks for more cases gets a stricter verdict, not more verdicts.
    """
    replay, identity = workspace.executable(executable)
    if replay is None:
        return SanitizeResponse.failure(
            f"the tree holds no executable '{executable}'; build it first"
        )

    per_tool = {}
    for tool in tools:
        errors = 0
        failed = False
        failing_log = ""
        last_log = ""
        unavailable = None
        for name, arrs in cases.items():
            cdir = _write_case(workspace.path("san", name), arrs)
            cmd = ["compute-sanitizer", "--tool", tool, "--error-exitcode", "1", replay, cdir]
            workspace.prepare_job_files()
            try:
                job = workspace.execute(
                    cmd, cwd=workspace.tree_dir, timeout=timeout, gpu=True,
                    what=f"the {tool} sanitizer on case '{name}'",
                )
            except ExecutionFailed as exc:
                # A tool that could not be run at all did not pass and did
                # not fail; the gateway is told which of the two it is.
                if exc.unavailable:
                    unavailable = str(exc)
                    break
                failed = True
                failing_log = str(exc)
                break
            output = job.stdout + job.stderr
            errors += len(re.findall(r"========= ERROR|Invalid|race", output))
            if not workspace.matches(replay, identity):
                failed = True
                failing_log = "the executable changed while it was being measured"
                break
            last_log = output[-1500:]
            if job.returncode != 0 and not failed:
                failed = True
                failing_log = last_log
        if unavailable is not None:
            per_tool[tool] = {"ok": None, "error": unavailable}
        else:
            # The log of the first case that failed, so the reader sees the
            # failure rather than whatever the last case happened to print.
            per_tool[tool] = {"ok": not failed, "errors": errors, "log_tail": failing_log or last_log}
    return SanitizeResponse(
        ok=bool(per_tool) and all(t.get("ok") is True for t in per_tool.values()),
        per_tool=per_tool, executable_identity=identity,
    )


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


def properties(workspace, executable, module, cases, seed, max_examples,
               *, harness_dir=HARNESS, timeout=PROPERTIES_TIMEOUT_S) -> PropertiesResponse:
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
    drawn = {"seed": seed, "max_examples": max_examples}
    replay, identity = workspace.executable(executable)
    if replay is None:
        return PropertiesResponse.failure(
            f"the tree holds no executable '{executable}'; build it first", **drawn,
        )

    module_path = workspace.in_tree(module)
    if module_path is None or not os.path.isfile(module_path):
        where = "does not stay inside the tree" if module_path is None else "is not in the tree"
        return PropertiesResponse.failure(
            f"the properties module '{module}' {where}", **drawn,
        )

    cases_dir = _write_dataset(workspace.path("property_cases"), cases)
    scratch = workspace.path("property_scratch")
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
    workspace.prepare_job_files()
    audited = workspace.policy.audited
    try:
        job = workspace.execute(
            command, cwd=workspace.tree_dir, env=env, timeout=timeout, gpu=True,
            mode="audited" if audited else "plain", what="the property run",
        )
    except ExecutionFailed as exc:
        return PropertiesResponse.failure(str(exc), **drawn)

    output = job.stdout + job.stderr
    counts = pytest_counts(output)
    replays_observed = None
    if audited:
        if not job.evidence or job.evidence.get("ok") is not True:
            return PropertiesResponse.failure(
                "protected replay execution evidence was unavailable", **drawn, **counts,
            )
        replay_job_path = "/job/tree/" + os.path.relpath(
            replay, workspace.tree_dir,
        ).replace(os.sep, "/")
        replays_observed = sum(
            1 for entry in job.evidence.get("executions", [])
            if isinstance(entry, dict) and entry.get("path") == replay_job_path
        )
        if replays_observed == 0:
            return PropertiesResponse.failure(
                "protected exec tracing observed no replay invocation",
                **drawn, **counts, replays_observed=0, executable_identity=identity,
            )
    if not workspace.matches(replay, identity):
        return PropertiesResponse.failure(
            "the executable changed while it was being measured",
            **drawn, **counts, executable_identity=identity,
        )
    return PropertiesResponse(
        ok=job.returncode == 0, **drawn, **counts,
        replays_observed=replays_observed,
        counts_source="pytest summary emitted by the submitted property process",
        executable_identity=identity,
        # Long enough to hold Hypothesis's minimized falsifying example,
        # which is the whole value of a failed property run.
        log_tail=output[-4000:],
    )


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
    everything it answers with is in the returned row -- including the
    workspace, which carries the policy the commands are run under, since
    a worker that was started rather than forked inherits nothing from
    this module. The mutant is built through the same make invocation the
    tree was, watched the same way, and its executable is identified
    before the first case and reidentified after each, so a verdict is
    about the binary that was built. Its directory is a copy of the tree
    that already built, so `make` rebuilds only what the changed file
    forces -- and it is removed again unless the verdict is one a person
    has to read the source of.
    """
    workspace = job["workspace"]
    mutant = job["mutant"]
    mutant_dir = job["mutant_dir"]
    deadline = time.monotonic() + job["timeout"]
    try:
        shutil.rmtree(mutant_dir, ignore_errors=True)
        shutil.copytree(workspace.tree_dir, mutant_dir, symlinks=True)
        _write_mutated(mutant_dir, mutant)
        workspace.prepare_job_files()

        try:
            made = _make(
                workspace, cwd=mutant_dir, makefile=job["makefile"],
                targets=[job["target"]], env=job["env"], timeout=_remaining(deadline),
                compiler=job["compiler"], flags=job["flags"],
                source_patterns=job["source_patterns"], harness_dir=job["harness_dir"],
                log_path=job["log_path"], what="the mutant's build",
            )
        except ExecutionFailed as exc:
            mutant.status = mutants.BUILD_FAIL
            mutant.note = (
                f"the mutant's build did not finish within {job['timeout']} seconds"
                if exc.timed_out else str(exc)
            )
            return mutant.as_result()
        replay = os.path.join(mutant_dir, job["executable"])
        if made.problem is not None:
            mutant.status = mutants.BUILD_FAIL
            mutant.note = made.problem
            return mutant.as_result()
        if made.returncode != 0 or not os.path.isfile(replay):
            mutant.status = mutants.BUILD_FAIL
            mutant.note = _last_line(made.output) or "make left no executable"
            return mutant.as_result()
        if not compile_log.flags_reached_every_compile(made.compiles):
            # A mutant built with other flags than the port faces is not a
            # statement about this gate, so it is not scored as one.
            mutant.status = mutants.BUILD_FAIL
            mutant.note = "the strategy's flags did not reach the mutant's compiles"
            return mutant.as_result()
        identity = workspace.identity_of(replay)

        for name in job["cases"]:
            case_dir = os.path.join(mutant_dir, "cases", name)
            shutil.rmtree(case_dir, ignore_errors=True)
            shutil.copytree(os.path.join(job["inputs_root"], name), case_dir)
            try:
                replayed = workspace.execute(
                    [replay, case_dir], cwd=mutant_dir, timeout=_remaining(deadline),
                    gpu=True, what=f"the mutant's replay of case '{name}'",
                )
            except ExecutionFailed as exc:
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = (
                    f"case '{name}': the replay did not finish in time"
                    if exc.timed_out else f"case '{name}': {exc}"
                )
                return mutant.as_result()
            if replayed.returncode != 0:
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = (
                    f"case '{name}': exit {replayed.returncode}: "
                    f"{_last_line(replayed.stdout + replayed.stderr)}"
                )
                return mutant.as_result()
            if not workspace.matches(replay, identity):
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = f"case '{name}': the mutant changed while it was being measured"
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
    shutil.rmtree(workspace.path("mutants"), ignore_errors=True)
    inputs_root = workspace.path("mutants", ".inputs")
    refs_root = workspace.path("mutants", ".refs")
    for name, case in cases.items():
        _write_case(os.path.join(inputs_root, name), case.get("inputs", {}))
        reference = os.path.join(refs_root, name)
        os.makedirs(reference, exist_ok=True)
        for variable, encoded in case.get("outputs", {}).items():
            with open(os.path.join(reference, f"{variable}{OUTPUT_SUFFIX}"), "wb") as out:
                out.write(base64.b64decode(encoded))
    return inputs_root, refs_root


def mutate(workspace, makefile, replay_target, files, cases, bands, compiler, flags,
           link_flags, source_patterns, *, jobs=None, limit=None,
           harness_dir=HARNESS, timeout=MUTATE_TIMEOUT_S,
           ceiling=MUTATE_CEILING_S) -> MutateResponse:
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

    Each result is one mutant and its verdict; the directories of the two
    verdicts a person has to read the source of are kept and named.
    """
    if not os.path.isdir(workspace.tree_dir):
        return MutateResponse.failure(
            f"there is no built tree for attempt '{workspace.attempt_id}'; "
            f"build it before mutating it"
        )

    generated = []
    for relative in files:
        path = workspace.in_tree(relative)
        if path is None or not os.path.isfile(path):
            where = "does not stay inside the tree" if path is None else "is not in the tree"
            return MutateResponse.failure(f"the region file '{relative}' {where}")
        if not compile_log.is_tree_source(relative, source_patterns):
            return MutateResponse.failure(
                f"the region file '{relative}' is not one this code calls its own source, "
                f"so mutating it would say nothing about a port of it"
            )
        with open(path, errors="replace") as source:
            generated.extend(mutants.generate(source.read(), relative))

    todo = generated[: int(limit)] if limit else list(generated)
    if not todo:
        return MutateResponse(
            ok=True, generated=len(generated),
            log_tail="no mutant was generated from the region's files",
        )

    inputs_root, refs_root = _mutation_corpus(workspace, cases)
    payloads = []
    for mutant in todo:
        # One shim log per mutant: mutants are built at the same time, and
        # a log they shared would say that some other mutant's compile was
        # this one's.
        log_path = workspace.path("mutants", f"{mutant.mid}.fc.jsonl")
        env = build_env(compiler, flags, link_flags, log_path, harness_dir)
        if workspace.policy.audited:
            env["FC"] = compiler
        payloads.append({
            "mutant": mutant,
            "workspace": workspace,
            "mutant_dir": workspace.path("mutants", mutant.mid),
            "makefile": makefile,
            "target": replay_target["target"],
            "executable": replay_target["executable"],
            "env": env,
            "log_path": log_path,
            "compiler": compiler,
            "flags": list(flags),
            "source_patterns": list(source_patterns),
            "harness_dir": harness_dir,
            "inputs_root": inputs_root,
            "refs_root": refs_root,
            "cases": sorted(cases),
            "bands": bands,
            "timeout": timeout,
        })

    workers = int(jobs) if jobs else min(DEFAULT_MUTATE_JOBS, os.cpu_count() or 1)
    results = _score_all(payloads, workers, ceiling)
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return MutateResponse(
        ok=True, generated=len(generated), scored=len(results),
        results=results, counts=counts,
        kept_dirs=[
            workspace.path("mutants", row["id"]) for row in results
            if row["status"] in mutants.KEEP_DIRECTORY
        ],
    )


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
        # Production executes the immutable binary from the frozen tree but
        # gives the application a fresh writable copy as its working directory.
        # This supports ordinary relative input/config files and arbitrary
        # scratch output without granting write access to the measured binary.
        if workspace.policy.owns_files:
            run_dir = workspace.path("timing", f"run-{repetition + 1:04d}")
            shutil.rmtree(run_dir, ignore_errors=True)
            shutil.copytree(workspace.tree_dir, run_dir, symlinks=True)
            workspace.prepare_job_files()
            workspace.writable_copy(run_dir)
        else:
            run_dir = workspace.tree_dir
        # A file left by an earlier run, or by an earlier attempt, would
        # otherwise be collected as if this run had written it.
        for relative in outputs:
            path = path_inside(run_dir, relative)
            if path is None:
                return TimeResponse.failure(
                    f"declared timing output '{relative}' leaves the tree",
                    runs_s=runs, outputs=collected,
                )
            if run_dir == workspace.tree_dir and (
                os.path.samefile(path, program) if os.path.exists(path) else path == program
            ):
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
