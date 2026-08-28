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

Writes: the code's tolerances.json, beside its manifest.

The two builds go through this code's own Makefile -- the same `replay`
target the builder asks for -- so what is measured is the binary the gates
run, compiled two ways.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(CODE))
sys.path.insert(0, REPO)

from equivalent.capture import npy  # noqa: E402  (found through the path added above)
from equivalent.manifest.schema import load_manifest  # noqa: E402

TREE = os.path.join(CODE, "baseline")
VIS_IN = os.path.join(CODE, "datasets", "visible")
MANIFEST = os.path.join(CODE, "manifest.yaml")
OUT = os.path.join(CODE, "tolerances.json")

PROFILES = {
    "plain":    ["-O2", "-ffree-line-length-none"],
    "fastmath": ["-O3", "-ffast-math", "-funroll-loops", "-ffree-line-length-none"],
}


def build(profile_flags, outbin):
    """One replay binary, built the way the harness builds it.

    `make clean` first because make would otherwise keep the binary the
    previous profile left: the sources have not changed, only the flags,
    and make cannot see that.
    """
    make = ["make", "-C", TREE, "FC=gfortran", f"FFLAGS={' '.join(profile_flags)}", "MODFLAG=-J"]
    subprocess.run([*make, "clean"], check=True)
    subprocess.run([*make, "replay"], check=True)
    shutil.copy(os.path.join(TREE, "replay"), outbin)
    subprocess.run([*make, "clean"], check=True)


def ulp_diff(a, b):
    """Distance in representable steps of a's own float type."""
    as_int = {np.float32: np.int32, np.float64: np.int64}[a.dtype.type]
    ia = a.view(as_int).astype(np.int64)
    ib = b.view(as_int).astype(np.int64)
    floor = np.int64(np.iinfo(as_int).min)
    ia = np.where(ia < 0, floor - ia, ia)
    ib = np.where(ib < 0, floor - ib, ib)
    return np.abs(ia - ib)


def main():
    manifest = load_manifest(MANIFEST)
    # The variables to calibrate are the ones the manifest declares as
    # outputs of the region, and only the floating-point ones: an integer
    # or logical output is compared exactly and has no band to set.
    variables = [v.name for v in manifest.interface.outputs if v.dtype in ("f32", "f64")]
    cases = npy.dataset_cases(VIS_IN)

    with tempfile.TemporaryDirectory() as tmp:
        bins = {}
        for name, flags in PROFILES.items():
            b = os.path.join(tmp, f"replay_{name}")
            build(flags, b)
            bins[name] = b

        obs = {v: {"abs": 0.0, "rel": 0.0, "ulp": 0} for v in variables}
        for c in cases:
            # run both builds on a private copy of this case's inputs
            outs = {}
            for name, b in bins.items():
                d = os.path.join(tmp, name, c)
                os.makedirs(d, exist_ok=True)
                for entry in os.listdir(os.path.join(VIS_IN, c)):
                    shutil.copy(os.path.join(VIS_IN, c, entry), d)
                subprocess.run([b, d], check=True)
                outs[name] = {
                    v: npy.decode(npy.output_path(d, v).read_bytes()) for v in variables
                }
            for v in variables:
                a, b = outs["plain"][v], outs["fastmath"][v]
                ad = np.abs(a - b)
                rd = ad / (np.abs(a) + np.finfo(a.dtype).tiny)
                obs[v]["abs"] = max(obs[v]["abs"], float(ad.max()))
                obs[v]["rel"] = max(obs[v]["rel"], float(rd.max()))
                obs[v]["ulp"] = max(obs[v]["ulp"], int(ulp_diff(a, b).max()))

    # bands: a few multiples above observed CPU spread, with floors
    MARGIN = 8
    ABS_FLOOR, REL_FLOOR, ULP_FLOOR = 1e-6, 1e-5, 16
    bands = {}
    for v in variables:
        bands[v] = {
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
        "variables": bands,
    }
    json.dump(policy, open(OUT, "w"), indent=2)
    print(json.dumps(policy, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
