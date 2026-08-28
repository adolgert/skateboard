"""Numerical comparator for the oracle. Pure numpy over binary float32 arrays.

Acceptance policy (per variable, per case): an element is acceptable if ANY of
its three metrics is within tolerance -- absolute, relative, or units-in-last-
place. A variable passes only if EVERY element is acceptable. A case passes only
if every variable passes. A dataset passes only if every case passes.

This module executes nothing supplied by the agent; it only reads arrays.
"""
import numpy as np


def _ulp_diff(ref: np.ndarray, got: np.ndarray) -> np.ndarray:
    """Distance in representable float32 steps, handling sign crossings."""
    a = ref.astype(np.float32).view(np.int32).astype(np.int64)
    b = got.astype(np.float32).view(np.int32).astype(np.int64)
    # map two's-complement int ordering to a monotonic ordering across zero
    a = np.where(a < 0, np.int64(np.iinfo(np.int32).min) - a, a)
    b = np.where(b < 0, np.int64(np.iinfo(np.int32).min) - b, b)
    return np.abs(a - b)


def compare_variable(ref: np.ndarray, got: np.ndarray, tol: dict) -> dict:
    ref = np.ascontiguousarray(ref, dtype=np.float32)
    got = np.ascontiguousarray(got, dtype=np.float32)
    if ref.shape != got.shape:
        return {"pass": False, "error": f"shape {got.shape} != expected {ref.shape}"}

    abs_err = np.abs(got - ref)
    rel_err = abs_err / (np.abs(ref) + 1e-30)
    ulp_err = _ulp_diff(ref, got)

    ok = (abs_err <= tol["abs"]) | (rel_err <= tol["rel"]) | (ulp_err <= tol["ulp"])
    return {
        "pass": bool(np.all(ok)),
        "max_abs": float(abs_err.max()),
        "max_rel": float(rel_err.max()),
        "max_ulp": int(ulp_err.max()),
        "n_bad": int((~ok).sum()),
        "n": int(ref.size),
    }


def compare_case(expected: dict, got: dict, tols: dict) -> dict:
    """expected/got: {'h': ndarray, 'u': ndarray}; tols: variables policy."""
    per_var = {}
    for var in expected:
        if var not in got:
            per_var[var] = {"pass": False, "error": "missing in output"}
        else:
            per_var[var] = compare_variable(expected[var], got[var], tols[var])
    return {"pass": all(v["pass"] for v in per_var.values()), "per_var": per_var}
