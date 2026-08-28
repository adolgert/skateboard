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

HARNESS = "/opt/harness"  # baked, trusted: mod_capture.f90, replay.f90

# Compile profiles. The orchestrator selects one per rung/baseline; the agent
# never supplies flags. cc89 = RTX 4000 Ada.
PROFILES = {
    "stdpar_managed":   {"fc": "nvfortran", "flags": ["-O2", "-stdpar=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"], "gpu": True,  "notify": "acc"},
    "omp_target":       {"fc": "nvfortran", "flags": ["-O2", "-mp=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"],     "gpu": True,  "notify": "omp"},
    "cpu_best":         {"fc": "nvfortran", "flags": ["-O2", "-stdpar=multicore"],                                    "gpu": False, "notify": None},
    "cpu_naive_stdpar": {"fc": "nvfortran", "flags": ["-O2", "-stdpar=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"], "gpu": True,  "notify": "acc"},
}

# Fixed source lists (dependency order). Content comes from the payload for the
# work-tree modules; mod_capture/replay come from the trusted baked copy.
REPLAY_PAYLOAD = ["mod_params.f90", "mod_diff.f90", "mod_kernel.f90"]
REPLAY_BAKED = [f"{HARNESS}/mod_capture.f90", f"{HARNESS}/replay.f90"]
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


def _count_kernels(stderr, notify):
    if notify in ("acc", "omp"):
        return len(re.findall(r"launch ", stderr))
    return 0


# net_jail (unshare -n) is defense-in-depth. It needs CAP_SYS_ADMIN, which the
# builder container does not hold by default, and build_net is already
# internal: true (no internet, no route to the oracle) -- so we leave it off
# tonight and turn it on as later hardening once the container has the cap.
def run(attempt_id, profile, cases, mandatory=False, net_jail=False):
    """cases: {name: {h_in: b64, u_in: b64}} -> replay each, return outputs + kernel count."""
    prof = PROFILES[profile]
    ws = _ws(attempt_id)
    replay = os.path.join(ws, "replay")
    if not os.path.exists(replay):
        return {"ok": False, "stage": "run", "log_tail": "replay binary not built"}

    env = _notify_env(os.environ, prof["notify"], mandatory)
    outputs = {}
    total_kernels = 0
    log_tail = ""
    for name, arrs in cases.items():
        cdir = os.path.join(ws, "cases", name)
        shutil.rmtree(cdir, ignore_errors=True)
        os.makedirs(cdir, exist_ok=True)
        for k in ("h_in", "u_in"):
            with open(os.path.join(cdir, f"{k}.bin"), "wb") as f:
                f.write(base64.b64decode(arrs[k]))
        # jail the child from the network (defense in depth; build_net is already internal)
        prefix = ["unshare", "-n", "--"] if net_jail else []
        try:
            rc, out, err = _run(prefix + [replay, cdir], cwd=ws, env=env, timeout=120)
        except Exception:
            rc, out, err = _run([replay, cdir], cwd=ws, env=env, timeout=120)  # unshare not permitted
        if rc != 0:
            return {"ok": False, "stage": "run", "case": name, "log_tail": (out + err)[-2000:]}
        total_kernels += _count_kernels(err, prof["notify"])
        log_tail = err[-1500:]
        with open(os.path.join(cdir, "h_out.bin"), "rb") as f:
            h_b = f.read()
        with open(os.path.join(cdir, "u_out.bin"), "rb") as f:
            u_b = f.read()
        outputs[name] = {"h": base64.b64encode(h_b).decode(), "u": base64.b64encode(u_b).decode()}

    return {"ok": True, "stage": "run", "outputs": outputs, "kernels_launched": total_kernels, "log_tail": log_tail}


def sanitize(attempt_id, profile, one_case, tools):
    prof = PROFILES[profile]
    ws = _ws(attempt_id)
    replay = os.path.join(ws, "replay")
    name, arrs = next(iter(one_case.items()))
    cdir = os.path.join(ws, "san", name)
    shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir, exist_ok=True)
    for k in ("h_in", "u_in"):
        with open(os.path.join(cdir, f"{k}.bin"), "wb") as f:
            f.write(base64.b64decode(arrs[k]))

    per_tool = {}
    for tool in tools:
        cmd = ["compute-sanitizer", "--tool", tool, "--error-exitcode", "1", replay, cdir]
        try:
            rc, out, err = _run(cmd, cwd=ws, timeout=600)
            errs = len(re.findall(r"========= ERROR|Invalid|race", out + err))
            per_tool[tool] = {"ok": rc == 0, "errors": errs, "log_tail": (out + err)[-1500:]}
        except FileNotFoundError:
            per_tool[tool] = {"ok": None, "error": "compute-sanitizer not found"}
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
