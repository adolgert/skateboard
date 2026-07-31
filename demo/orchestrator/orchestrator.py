"""Orchestrator: the trusted core and the only initiator.

Runs the deterministic staged loop for one region (ch04's `step`):
  baselines -> for each rung -> for each attempt:
      agent -> build -> device-proof -> sanitize -> compare(visible) -> time
  on all-pass: acceptance on the held-out set, then stop.

Every attempt is a git branch and one append-only ledger row. The orchestrator
carries every byte between components; no component talks to any other directly.
"""
import base64
import csv
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

import requests

AGENT = os.environ.get("AGENT_URL", "http://agent-runner:8080")
BUILDER = os.environ.get("BUILDER_URL", "http://builder:9090")
ORACLE = os.environ.get("ORACLE_URL", "http://oracle:7070")
TOKEN = os.environ.get("SKATEBOARD_TOKEN", "")
HDR = {"Authorization": f"Bearer {TOKEN}"}

REPO = "/repo"
SEED = "/seed"                       # pristine work-tree baked into the image
VISIBLE = "/datasets/visible"       # visible inputs baked in
LEDGER = "/ledger/ledger.csv"
REGION = "ch04:step"
RUNGS = os.environ.get("RUNGS", "stdpar_managed").split(",")
MODEL_KEYS = os.environ.get("MODEL_KEYS", "haiku,sonnet,gemini-flash,qwen").split(",")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
EDITABLE = "src/mod_kernel.f90"

LEDGER_COLS = [
    "ts", "region", "model_key", "rung", "attempt", "backend", "model", "branch", "src_sha",
    "oracle_policy_sha", "build", "device_proof", "kernels_launched",
    "memcheck", "racecheck", "initcheck", "compare_visible", "compare_holdout",
    "cpu_best_s", "naive_stdpar_s", "port_s", "speedup", "verdict",
    "human_intervention", "notes",
]


def sh(*args, cwd=REPO):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def log(msg):
    print(f"[orchestrator] {msg}", flush=True)


def post(url, payload):
    r = requests.post(url, headers=HDR, json=payload, timeout=1200)
    r.raise_for_status()
    return r.json()


def get(url):
    r = requests.get(url, headers=HDR, timeout=300)
    r.raise_for_status()
    return r.json()


def init_repo():
    # /repo is a mounted volume: clear its CONTENTS (not the mountpoint itself),
    # then copy the pristine seed in.
    subprocess.run("rm -rf /repo/* /repo/.[!.]* 2>/dev/null; true", shell=True)
    subprocess.run(["cp", "-r", f"{SEED}/.", f"{REPO}/"], check=True)
    sh("init", "-q")
    sh("config", "user.email", "orchestrator@skateboard")
    sh("config", "user.name", "orchestrator")
    sh("add", "-A")
    sh("commit", "-q", "-m", "baseline (pristine ch04)")
    sh("branch", "-M", "main")


def snapshot():
    files = []
    for p in sorted(glob.glob(f"{REPO}/src/*.f90")):
        files.append({"path": "src/" + os.path.basename(p), "content": open(p).read()})
    return files


def src_sha(files):
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x["path"]):
        h.update(f["path"].encode())
        h.update(f["content"].encode())
    return h.hexdigest()[:12]


def load_cases(dirpath):
    cases = json.load(open(f"{dirpath}/cases.json"))["cases"]
    out = {}
    for c in cases:
        out[c] = {
            "h_in": base64.b64encode(open(f"{dirpath}/{c}/h_in.bin", "rb").read()).decode(),
            "u_in": base64.b64encode(open(f"{dirpath}/{c}/u_in.bin", "rb").read()).decode(),
        }
    return out


def ledger_write(row):
    newfile = not os.path.exists(LEDGER)
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
        if newfile:
            w.writeheader()
        w.writerow({k: row.get(k, "NA") for k in LEDGER_COLS})


def run_baselines(pristine, visible):
    """Best-CPU and naive-stdpar floors, measured before the agent runs."""
    results = {}
    for name, profile in [("cpu_best", "cpu_best"), ("naive_stdpar", "cpu_naive_stdpar")]:
        try:
            b = post(f"{BUILDER}/v1/build", {"attempt_id": f"baseline-{name}", "source": {"files": pristine}, "profile": profile})
            if not b.get("ok"):
                log(f"baseline {name} build failed: {b.get('log_tail','')[:200]}")
                results[name] = None
                continue
            t = post(f"{BUILDER}/v1/time", {"attempt_id": f"baseline-{name}", "repeats": 5})
            results[name] = min(t["runs_s"]) if t.get("ok") else None
            log(f"baseline {name}: {results[name]} s (min of {t.get('runs_s')})")
        except Exception as e:
            log(f"baseline {name} error: {e}")
            results[name] = None
    return results


def attempt(rung, n, pristine, visible, policy_sha, baselines, prev_failure, model_key):
    aid = f"{model_key}-{rung}-{n}"
    branch = f"attempt/{aid}"
    row = {"ts": _now(), "region": REGION, "model_key": model_key, "rung": rung, "attempt": n,
           "oracle_policy_sha": policy_sha,
           "cpu_best_s": baselines.get("cpu_best"), "naive_stdpar_s": baselines.get("naive_stdpar"),
           "human_intervention": 0, "verdict": "fail"}

    # fresh branch from pristine baseline
    sh("checkout", "-q", "main")
    sh("checkout", "-q", "-B", branch)
    # restore pristine kernel so each attempt starts clean
    open(f"{REPO}/{EDITABLE}", "w").write(next(f["content"] for f in pristine if f["path"] == EDITABLE))

    # 1) agent produces the new kernel
    a = post(f"{AGENT}/v1/attempt", {"attempt_id": aid, "strategy": rung, "model_key": model_key,
                                     "files": pristine, "failure_report": prev_failure})
    row["backend"] = a.get("backend"); row["model"] = a.get("model_id"); row["notes"] = (a.get("notes") or "")[:120]
    for f in a["files"]:
        if f["path"] != EDITABLE:
            log(f"REJECTED agent write to {f['path']} (not allowed)")
            row["notes"] = "rejected disallowed path"
            ledger_write(row); return None, row
        open(f"{REPO}/{f['path']}", "w").write(f["content"])
    sh("add", "-A"); sh("commit", "-q", "-m", f"{aid}: agent port")
    snap = snapshot()
    row["src_sha"] = src_sha(snap); row["branch"] = branch
    agent_code = next((f["content"] for f in a["files"] if f["path"] == EDITABLE), "")

    def fail(stage, detail):
        # carry the agent's own failing code forward so the next attempt repairs
        # its own work rather than re-porting the pristine source from scratch.
        return {"stage_failed": stage, "detail": detail, "previous_code": agent_code}, row

    # 2) build gate
    b = post(f"{BUILDER}/v1/build", {"attempt_id": aid, "source": {"files": snap}, "profile": rung})
    if not b.get("ok"):
        row["build"] = "fail"; ledger_write(row)
        return fail("build", {"compiler_errors": b.get("log_tail", "")})
    row["build"] = "pass"

    # 3) run + device-execution proof
    runr = post(f"{BUILDER}/v1/run", {"attempt_id": aid, "profile": rung, "cases": visible,
                                      "mandatory": rung == "omp_target"})
    if not runr.get("ok"):
        row["device_proof"] = "fail"; ledger_write(row)
        return fail("run", {"log": runr.get("log_tail", "")})
    kern = runr.get("kernels_launched", 0); row["kernels_launched"] = kern
    if kern <= 0:
        row["device_proof"] = "fail"; ledger_write(row)
        return fail("device_proof", {"kernels_launched": 0, "hint": "code compiled but no GPU kernel launched; ensure the loops are do concurrent / omp target so nvfortran offloads them"})
    row["device_proof"] = "pass"

    # 4) sanitizer
    one = {list(visible)[0]: visible[list(visible)[0]]}
    san = post(f"{BUILDER}/v1/sanitize", {"attempt_id": aid, "profile": rung, "case": one,
                                          "tools": ["memcheck", "racecheck", "initcheck"]})
    pt = san.get("per_tool", {})
    row["memcheck"] = _tool(pt, "memcheck"); row["racecheck"] = _tool(pt, "racecheck"); row["initcheck"] = _tool(pt, "initcheck")
    if pt.get("memcheck", {}).get("ok") is False or pt.get("racecheck", {}).get("ok") is False:
        ledger_write(row)
        return fail("sanitize", {k: v.get("log_tail", "")[:400] for k, v in pt.items() if v.get("ok") is False})

    # 5) correctness on the visible set
    cmp = post(f"{ORACLE}/v1/compare", {"attempt_id": aid, "dataset": "visible", "outputs": runr["outputs"]})
    row["compare_visible"] = cmp["verdict"]
    if cmp["verdict"] != "pass":
        ledger_write(row)
        return fail("compare", {"dataset": "visible", "per_case": _worst(cmp.get("per_case", {}))})

    # 6) performance
    t = post(f"{BUILDER}/v1/time", {"attempt_id": aid, "repeats": 5})
    port_s = min(t["runs_s"]) if t.get("ok") else None
    row["port_s"] = port_s
    cpu_best = baselines.get("cpu_best")
    if port_s and cpu_best:
        row["speedup"] = round(cpu_best / port_s, 3)

    # 7) acceptance on the held-out set (once). The held-out INPUTS come from the
    # oracle; they are fed to the SAME already-built binary (same attempt_id
    # workspace), so no rebuild and the agent never saw these inputs.
    hi = get(f"{ORACLE}/v1/dataset/holdout/inputs")["cases"]
    hruns = post(f"{BUILDER}/v1/run", {"attempt_id": aid, "profile": rung, "cases": hi})
    hcmp = post(f"{ORACLE}/v1/compare", {"attempt_id": aid, "dataset": "holdout", "outputs": hruns["outputs"]})
    row["compare_holdout"] = hcmp["verdict"]
    row["verdict"] = "ACCEPTED" if hcmp["verdict"] == "pass" else "holdout_fail"
    ledger_write(row)
    if hcmp["verdict"] == "pass":
        return None, row  # accepted
    return fail("compare", {"dataset": "holdout", "note": "passed visible but failed held-out"})


def _tool(pt, name):
    v = pt.get(name, {})
    if v.get("ok") is True:
        return "pass"
    if v.get("ok") is False:
        return "fail"
    return "na"


def _worst(per_case):
    out = {}
    for c, r in per_case.items():
        pv = r.get("per_var", {})
        out[c] = {k: {kk: v.get(kk) for kk in ("max_abs", "max_rel", "max_ulp", "pass")} for k, v in pv.items()}
    return out


def _has_main():
    r = subprocess.run(["git", "branch", "--list", "main"], cwd=REPO, capture_output=True, text=True)
    return "main" in r.stdout


_now_counter = [0]
def _now():
    # Date.now() is unavailable; use a monotonic-ish counter + env stamp for ordering
    _now_counter[0] += 1
    return f"{os.environ.get('RUN_STAMP','run')}-{_now_counter[0]:04d}"


def wait_ready():
    targets = {"agent-runner": AGENT, "builder": BUILDER, "oracle": ORACLE}
    for name, url in targets.items():
        for i in range(60):
            try:
                requests.get(f"{url}/healthz", headers=HDR, timeout=5).raise_for_status()
                log(f"{name} ready")
                break
            except Exception:
                if i == 0:
                    log(f"waiting for {name} ...")
                time.sleep(2)
        else:
            raise RuntimeError(f"{name} never became ready at {url}")


def main():
    wait_ready()
    log("initializing repo from pristine seed")
    init_repo()
    pristine = snapshot()
    visible = load_cases(VISIBLE)
    policy = get(f"{ORACLE}/v1/policy")
    policy_sha = policy["policy_sha256"][:12]
    log(f"oracle policy {policy['policy_version']} sha={policy_sha}")

    log("measuring baselines (before agent runs)")
    baselines = run_baselines(pristine, visible)

    log(f"campaign models: {MODEL_KEYS}   rungs: {RUNGS}   max_attempts: {MAX_ATTEMPTS}")
    campaign = []
    for model_key in MODEL_KEYS:
        log(f"########## MODEL {model_key} ##########")
        accepted = False
        last = {}
        for rung in RUNGS:
            prev_failure = None
            for n in range(1, MAX_ATTEMPTS + 1):
                log(f"=== [{model_key}] rung {rung} attempt {n}/{MAX_ATTEMPTS} ===")
                try:
                    failure, row = attempt(rung, n, pristine, visible, policy_sha, baselines, prev_failure, model_key)
                except Exception as e:
                    log(f"attempt error: {e}")
                    prev_failure = {"stage_failed": "harness", "detail": {"error": str(e)}}
                    last = {"verdict": "error"}
                    continue
                last = row
                log(f"  -> {row.get('verdict')} build={row.get('build')} device={row.get('device_proof')} "
                    f"compare={row.get('compare_visible')} speedup={row.get('speedup')}")
                if failure is None:
                    accepted = True
                    break
                prev_failure = failure
            if accepted:
                break
        campaign.append((model_key, accepted, last))
        log(f"########## {model_key}: {'ACCEPTED' if accepted else 'NOT ACCEPTED'} ##########")

    log("===== CAMPAIGN SUMMARY =====")
    log(f"baselines: cpu_best={baselines.get('cpu_best')}  naive_stdpar={baselines.get('naive_stdpar')}")
    for model_key, accepted, row in campaign:
        log(f"  {model_key:14s} {'ACCEPTED' if accepted else 'failed  '}  "
            f"attempt={row.get('attempt','-')} rung={row.get('rung','-')} "
            f"speedup={row.get('speedup','-')} "
            f"stages=build:{row.get('build','-')}/dev:{row.get('device_proof','-')}/cmp:{row.get('compare_visible','-')}")
    log("ledger: " + LEDGER)
    sys.exit(0)


if __name__ == "__main__":
    main()
