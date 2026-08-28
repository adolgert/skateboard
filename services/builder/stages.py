"""Baked gate logic for the builder. THIS is the file the architecture means by
"the exact set of things that can run on the GPU is enumerable in one file."

The agent's patch can change only Fortran source content. It can NOT change any
command line, flag, or environment variable here -- those are part of the image.
"""
import base64
import glob
import os
import re
import shutil
import subprocess
import time

HARNESS = "/opt/harness"  # baked, trusted: npy_io.f90, replay.f90

# The capture format on disk: one file per variable in the case directory,
# <variable>.npy going in and <variable>.out.npy coming out. Each file says
# for itself what type and shape it holds, so this service never has to be
# told a variable name or an element type.
INPUT_SUFFIX = ".npy"
OUTPUT_SUFFIX = ".out.npy"

# Compile profiles. The orchestrator selects one per rung/baseline; the agent
# never supplies flags. cc89 = RTX 4000 Ada.
PROFILES = {
    "stdpar_managed":   {"fc": "nvfortran", "flags": ["-O2", "-stdpar=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"], "gpu": True,  "notify": "acc"},
    "omp_target":       {"fc": "nvfortran", "flags": ["-O2", "-mp=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"],     "gpu": True,  "notify": "omp"},
    "cpu_best":         {"fc": "nvfortran", "flags": ["-O2", "-stdpar=multicore"],                                    "gpu": False, "notify": None},
    "cpu_naive_stdpar": {"fc": "nvfortran", "flags": ["-O2", "-stdpar=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"], "gpu": True,  "notify": "acc"},
}

# Fixed source lists (dependency order). Content comes from the payload for the
# work-tree modules; npy_io/replay come from the trusted baked copy.
REPLAY_PAYLOAD = ["mod_params.f90", "mod_diff.f90", "mod_kernel.f90"]
REPLAY_BAKED = [f"{HARNESS}/npy_io.f90", f"{HARNESS}/replay.f90"]
TSUNAMI_PAYLOAD = ["mod_params.f90", "mod_diff.f90", "mod_initial.f90", "mod_kernel.f90", "tsunami.f90"]


def _ws(attempt_id):
    d = os.path.join("/work", re.sub(r"[^A-Za-z0-9._-]", "_", attempt_id))
    return d


def _write_sources(ws, files):
    """files: [{path, content}] -> write basenames into ws/src, return basename set."""
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    have = {}
    for f in files:
        base = os.path.basename(f["path"])
        with open(os.path.join(src, base), "w") as out:
            out.write(f["content"])
        have[base] = True
    return src, have


def _run(cmd, cwd=None, env=None, timeout=300):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def build(attempt_id, files, profile, flags=None, link_flags=None):
    # `flags`/`link_flags`, when given, come from the gateway's hashed
    # strategy file and replace this profile's baked flag list. The
    # agent still can't reach this endpoint; only the gateway can.
    prof = PROFILES[profile]
    used_flags = list(flags) if flags is not None else list(prof["flags"])
    used_flags += list(link_flags or [])
    ws = _ws(attempt_id)
    shutil.rmtree(ws, ignore_errors=True)
    src, have = _write_sources(ws, files)

    missing = [s for s in REPLAY_PAYLOAD if s not in have]
    if missing:
        return {"ok": False, "stage": "build", "log_tail": f"missing sources: {missing}"}

    env = dict(os.environ)
    logs = []
    # replay binary (region-level)
    replay_srcs = [os.path.join(src, s) for s in REPLAY_PAYLOAD] + REPLAY_BAKED
    rc, out, err = _run([prof["fc"], *used_flags, "-module", ws, "-o", os.path.join(ws, "replay"), *replay_srcs], cwd=ws, env=env)
    logs.append(("replay", rc, out, err))
    if rc != 0:
        return {"ok": False, "stage": "build", "target": "replay", "flags": used_flags, "log_tail": (out + err)[-4000:]}

    # end-to-end binary (for timing) -- best effort; only required for /time
    tsu_ok = all(s in have for s in TSUNAMI_PAYLOAD)
    if tsu_ok:
        tsu_srcs = [os.path.join(src, s) for s in TSUNAMI_PAYLOAD]
        rc2, out2, err2 = _run([prof["fc"], *used_flags, "-module", ws, "-o", os.path.join(ws, "tsunami"), *tsu_srcs], cwd=ws, env=env)
        logs.append(("tsunami", rc2, out2, err2))

    minfo = "\n".join(l[3] for l in logs)  # -Minfo=accel goes to stderr
    accel_lines = [ln for ln in minfo.splitlines() if "Generating" in ln or "Loop" in ln or "GPU" in ln]
    return {
        "ok": True,
        "stage": "build",
        "profile": profile,
        "flags": used_flags,  # what was actually passed to the compiler
        "minfo_excerpt": "\n".join(accel_lines[:40]),
        "log_tail": minfo[-2000:],
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


# net_jail (unshare -n) is defense-in-depth. It needs CAP_SYS_ADMIN, which the
# builder container does not hold by default, and build_net is already
# internal: true (no internet, no route to the oracle) -- so we leave it off
# tonight and turn it on as later hardening once the container has the cap.
def run(attempt_id, profile, cases, mandatory=False, net_jail=False):
    """cases: {name: {variable: b64 npy}} -> replay each, return outputs + kernel count."""
    prof = PROFILES[profile]
    ws = _ws(attempt_id)
    replay = os.path.join(ws, "replay")
    if not os.path.exists(replay):
        return {"ok": False, "stage": "run", "log_tail": "replay binary not built"}

    env = _notify_env(os.environ, prof["notify"], mandatory)
    outputs = {}
    total_kernels = 0
    launched_at = set()
    log_tail = ""
    for name, arrs in cases.items():
        cdir = _write_case(os.path.join(ws, "cases", name), arrs)
        # jail the child from the network (defense in depth; build_net is already internal)
        prefix = ["unshare", "-n", "--"] if net_jail else []
        try:
            rc, out, err = _run(prefix + [replay, cdir], cwd=ws, env=env, timeout=120)
        except Exception:
            rc, out, err = _run([replay, cdir], cwd=ws, env=env, timeout=120)  # unshare not permitted
        if rc != 0:
            return {"ok": False, "stage": "run", "case": name, "log_tail": (out + err)[-2000:]}
        kernels, launches = kernel_launches(err, prof["notify"])
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


def sanitize(attempt_id, profile, cases, tools):
    """cases: {name: {variable: b64 npy}} -> run every tool over every case.

    The caller chooses how many cases to send; whether that is one or all
    of them is the strategy's decision, not this file's. There is still
    one entry per tool in the response: the error counts are summed over
    the cases and a tool fails if it failed on any of them, so a caller
    that asks for more cases gets a stricter verdict, not more verdicts.
    """
    prof = PROFILES[profile]
    ws = _ws(attempt_id)
    replay = os.path.join(ws, "replay")

    per_tool = {}
    for tool in tools:
        errors = 0
        failed = False
        failing_log = ""
        last_log = ""
        unavailable = None
        for name, arrs in cases.items():
            cdir = _write_case(os.path.join(ws, "san", name), arrs)
            cmd = ["compute-sanitizer", "--tool", tool, "--error-exitcode", "1", replay, cdir]
            try:
                rc, out, err = _run(cmd, cwd=ws, timeout=600)
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


def time_run(attempt_id, repeats=5):
    ws = _ws(attempt_id)
    tsu = os.path.join(ws, "tsunami")
    if not os.path.exists(tsu):
        return {"ok": False, "stage": "time", "log_tail": "tsunami binary not built"}
    # honest timing wants exclusive GPU
    gpu_excl = _gpu_exclusive()
    # The pristine kernel returns difference arrays by value, so the CPU baseline
    # allocates temporaries every step; tell glibc to retain the arena so we time
    # the stencil, not page faults (see codes/tsunami/src/ch04_large/README.md).
    env = dict(os.environ)
    env["MALLOC_TRIM_THRESHOLD_"] = "-1"
    env["MALLOC_MMAP_THRESHOLD_"] = "1073741824"
    runs = []
    last = ""
    for _ in range(repeats):
        t0 = time.monotonic()
        rc, out, err = _run([tsu], cwd=ws, env=env, timeout=300)
        runs.append(time.monotonic() - t0)
        last = out.strip()
        if rc != 0:
            return {"ok": False, "stage": "time", "log_tail": (out + err)[-1500:]}
    return {"ok": True, "stage": "time", "runs_s": runs, "gpu_exclusive": gpu_excl, "diagnostic": last}


def _gpu_exclusive():
    try:
        rc, out, err = _run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"], timeout=15)
        procs = [l for l in out.strip().splitlines() if l.strip()]
        return len(procs) == 0
    except Exception:
        return None
