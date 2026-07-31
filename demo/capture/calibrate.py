#!/usr/bin/env python3
"""Calibrate per-variable tolerances from the CPU's own numerical spread.

We compile the replay driver two ways with gfortran -- a plain build and an
aggressive fast-math build -- run both over the visible input cases, and measure
how much the two CPU builds already disagree. Acceptance bands are set a few
multiples above that observed spread, with sane floors. This is the honest,
cheap calibration from skateboard.tex: we measure the sensitivity the code
already exhibits and accept differences of that magnitude.

Re-run against nvfortran once the HPC SDK image is available to widen bands if
the GPU's FMA/reduction reordering exceeds the CPU spread (a real finding).

Writes: oracle/tolerances.json
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
WORK = os.path.join(DEMO, "work", "src")
VIS_IN = os.path.join(DEMO, "orchestrator", "datasets", "visible")
OUT = os.path.join(DEMO, "oracle", "tolerances.json")

KMODS = [f"{WORK}/mod_params.f90", f"{WORK}/mod_diff.f90",
         f"{WORK}/mod_initial.f90", f"{WORK}/mod_kernel.f90"]
CAP = f"{HERE}/mod_capture.f90"
REPLAY = f"{HERE}/replay.f90"

PROFILES = {
    "plain":    ["-O2", "-ffree-line-length-none"],
    "fastmath": ["-O3", "-ffast-math", "-funroll-loops", "-ffree-line-length-none"],
}


def build(profile_flags, outbin, tmp):
    cmd = ["gfortran", *profile_flags, "-J", tmp, "-o", outbin, *KMODS, CAP, REPLAY]
    subprocess.run(cmd, check=True, cwd=tmp)


def read_f32(path):
    with open(path, "rb") as f:
        data = f.read()
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


def f32_bits(x):
    return struct.unpack("<i", struct.pack("<f", x))[0]


def ulp_diff(a, b):
    ia, ib = f32_bits(a), f32_bits(b)
    if (ia < 0) != (ib < 0):  # opposite signs: distance across zero
        return abs(ia) + abs(ib)
    return abs(ia - ib)


def main():
    cases = json.load(open(os.path.join(VIS_IN, "cases.json")))["cases"]
    with tempfile.TemporaryDirectory() as tmp:
        bins = {}
        for name, flags in PROFILES.items():
            b = os.path.join(tmp, f"replay_{name}")
            build(flags, b, tmp)
            bins[name] = b

        obs = {"h": {"abs": 0.0, "rel": 0.0, "ulp": 0}, "u": {"abs": 0.0, "rel": 0.0, "ulp": 0}}
        for c in cases:
            # run both builds on a private copy of this case's inputs
            outs = {}
            for name, b in bins.items():
                d = os.path.join(tmp, name, c)
                os.makedirs(d, exist_ok=True)
                shutil.copy(os.path.join(VIS_IN, c, "h_in.bin"), d)
                shutil.copy(os.path.join(VIS_IN, c, "u_in.bin"), d)
                subprocess.run([b, d], check=True)
                outs[name] = {v: read_f32(os.path.join(d, f"{v}_out.bin")) for v in ("h", "u")}
            for v in ("h", "u"):
                a_list, b_list = outs["plain"][v], outs["fastmath"][v]
                for a, bb in zip(a_list, b_list):
                    ad = abs(a - bb)
                    rd = ad / (abs(a) + 1e-30)
                    obs[v]["abs"] = max(obs[v]["abs"], ad)
                    obs[v]["rel"] = max(obs[v]["rel"], rd)
                    obs[v]["ulp"] = max(obs[v]["ulp"], ulp_diff(a, bb))

    # bands: a few multiples above observed CPU spread, with floors for float32
    MARGIN = 8
    ABS_FLOOR, REL_FLOOR, ULP_FLOOR = 1e-6, 1e-5, 16
    variables = {}
    for v in ("h", "u"):
        variables[v] = {
            "abs": max(ABS_FLOOR, MARGIN * obs[v]["abs"]),
            "rel": max(REL_FLOOR, MARGIN * obs[v]["rel"]),
            "ulp": int(max(ULP_FLOOR, MARGIN * obs[v]["ulp"])),
        }

    policy = {
        "policy_version": "2026-07-30-cpuspread-v1",
        "calibration": {
            "method": "gfortran -O2 vs -O3 -ffast-math -funroll-loops, visible cases",
            "margin_over_observed": MARGIN,
            "floors": {"abs": ABS_FLOOR, "rel": REL_FLOOR, "ulp": ULP_FLOOR},
            "observed_cpu_spread": obs,
            "note": "Recalibrate against nvfortran once the HPC SDK image is available.",
        },
        "acceptance": "A variable passes if (max_abs <= abs) OR (max_rel <= rel) OR (max_ulp <= ulp).",
        "variables": variables,
    }
    json.dump(policy, open(OUT, "w"), indent=2)
    print(json.dumps(policy, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
