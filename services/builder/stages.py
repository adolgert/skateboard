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
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import contract, mutate as mutants

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

    env = build_env(compiler, flags, link_flags, log_path, harness_dir)
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
    with open(os.path.join(case_dir, CASE_FILE)) as f:
        listed = json.load(f)
    case = {}
    for section, suffix in (("inputs", INPUT_SUFFIX), ("outputs", OUTPUT_SUFFIX)):
        arrays = {}
        for name in listed.get(section, []):
            if not _plain_name(name):
                raise ValueError(f"{CASE_FILE} lists {name!r}, which is not a variable name")
            path = os.path.join(case_dir, f"{name}{suffix}")
            if os.path.exists(path):
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
    tree_dir, program = _executable(attempt_id, executable, work_root)
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
        rc, out, err = _run([program, *args, outdir], cwd=tree_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "stage": "capture", "cases": {},
            "stdout_tail": f"the capture run did not finish within {timeout} seconds",
        }
    tail = (out + err)[-2000:]
    if rc != 0:
        return {"ok": False, "stage": "capture", "cases": {}, "stdout_tail": tail}

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
    return {"ok": True, "stage": "capture", "cases": cases, "stdout_tail": tail}


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
COUNT_PATTERN = re.compile(r"(\d+)\s+(passed|failed|errors?)\b")


def pytest_counts(text) -> dict:
    """{passed, failed, errors} as pytest's summary reported them.

    The last figure for each word wins: pytest writes its summary at the
    end, and a failing test's own captured output can hold anything.
    """
    counts = {"passed": 0, "failed": 0, "errors": 0}
    for match in COUNT_PATTERN.finditer(text):
        word = match.group(2)
        counts["errors" if word.startswith("error") else word] = int(match.group(1))
    return counts


def _in_tree(tree_dir, relative):
    """The absolute path of a file the manifest named, or None if it left the tree.

    A properties module is a path out of the code's own manifest, so it
    gets the same treatment as a submitted tree path: one that climbs out
    of the tree, or is absolute, names a file this service will not run.
    """
    tree_dir = os.path.abspath(tree_dir)
    path = os.path.normpath(os.path.join(tree_dir, relative))
    if path != tree_dir and not path.startswith(tree_dir + os.sep):
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
    tree_dir, replay = _executable(attempt_id, executable, work_root)
    if replay is None:
        return {
            "ok": False, "stage": "properties", "seed": seed, "max_examples": max_examples,
            "passed": 0, "failed": 0, "errors": 0,
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    module_path = _in_tree(tree_dir, module)
    if module_path is None or not os.path.isfile(module_path):
        where = "does not stay inside the tree" if module_path is None else "is not in the tree"
        return {
            "ok": False, "stage": "properties", "seed": seed, "max_examples": max_examples,
            "passed": 0, "failed": 0, "errors": 0,
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
    try:
        rc, out, err = _run(command, cwd=tree_dir, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "stage": "properties", "seed": seed, "max_examples": max_examples,
            "passed": 0, "failed": 0, "errors": 0,
            "log_tail": f"the property run did not finish within {timeout} seconds",
        }

    output = out + err
    return {
        "ok": rc == 0, "stage": "properties", "seed": seed, "max_examples": max_examples,
        **pytest_counts(output),
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
    everything it answers with is in the returned row. The mutant's
    directory is a copy of the tree that already built, so `make` rebuilds
    only what the changed file forces -- and it is removed again unless
    the verdict is one a person has to read the source of.
    """
    mutant = job["mutant"]
    mutant_dir = job["mutant_dir"]
    deadline = time.monotonic() + job["timeout"]
    try:
        shutil.rmtree(mutant_dir, ignore_errors=True)
        shutil.copytree(job["tree_dir"], mutant_dir, symlinks=True)
        _write_mutated(mutant_dir, mutant)

        command = ["make", "-f", job["makefile"], job["target"]]
        try:
            rc, out, err = _run(
                command, cwd=mutant_dir, env=job["env"], timeout=_remaining(deadline),
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
    payloads = [
        {
            "mutant": mutant,
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
    pool = ProcessPoolExecutor(max_workers=max(1, workers))
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
             budget_s=300, *, work_root=WORK_ROOT) -> dict:
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
    tree_dir, program = _executable(attempt_id, executable, work_root)
    if program is None:
        return {
            "ok": False, "stage": "time",
            "log_tail": f"the tree holds no executable '{executable}'; build it first",
        }

    run_env = {**os.environ, **{str(k): str(v) for k, v in (env or {}).items()}}
    # honest timing wants exclusive GPU
    gpu_excl = _gpu_exclusive()
    runs = []
    collected = []
    last = ""
    for _ in range(repeats):
        # A file left by an earlier run, or by an earlier attempt, would
        # otherwise be collected as if this run had written it.
        for relative in outputs:
            path = os.path.join(tree_dir, relative)
            if os.path.exists(path):
                os.remove(path)

        t0 = time.monotonic()
        try:
            rc, out, err = _run([program, *args], cwd=tree_dir, env=run_env, timeout=budget_s)
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

        this_run = {}
        for relative in outputs:
            path = os.path.join(tree_dir, relative)
            if not os.path.exists(path):
                return {
                    "ok": False, "stage": "time", "runs_s": runs, "outputs": collected,
                    "log_tail": f"run {len(runs)} wrote no '{relative}', which the "
                                f"manifest declares",
                }
            with open(path, "rb") as f:
                this_run[relative] = base64.b64encode(f.read()).decode()
        collected.append(this_run)

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
