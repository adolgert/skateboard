"""Numerical comparator. Pure numpy over whatever arrays it is given.

Acceptance policy for a floating-point variable, per case: an element is
acceptable if ANY of its three metrics is within tolerance -- absolute,
relative, or units-in-last-place. Integer and logical variables carry no
tolerance at all: any differing element fails. A variable passes only if
EVERY element is acceptable, a case passes only if every variable passes,
and a dataset passes only if every case passes.

Nothing here knows a variable's name, its element type, or its rank in
advance: both arrays come from files that say what they are, and this
module compares them or refuses to.

Trust role: this is the last word on every comparison the harness makes,
and there is one of it. The oracle asks it whether a replay reproduced
the captured answers; the gateway asks it whether a port's whole-program
run reproduced the baseline program's files. Two comparators would be
two definitions of "the same answer", and a port could pass under one of
them and not the other.

This module executes nothing supplied by the agent; it only reads arrays.
It lives here, beside the capture format whose files it compares, and is
copied into the sealed oracle image, which holds numpy and yaml and
nothing else of this project -- so it imports neither, and imports
nothing of this package either.
"""
import numpy as np

# The signed integer type of the same width as each floating-point type,
# so that the distance between two floats can be counted in representable
# steps by walking their bit patterns.
ULP_INT = {4: np.int32, 8: np.int64}


def _ulp_diff(ref: np.ndarray, got: np.ndarray) -> np.ndarray:
    """Distance in representable steps of the arrays' own type, across sign changes."""
    as_int = ULP_INT[ref.dtype.itemsize]
    floor = np.int64(np.iinfo(as_int).min)
    ordered = []
    for array in (ref, got):
        bits = array.view(as_int).astype(np.int64)
        # Map two's-complement ordering to one that runs monotonically
        # from the most negative float to the most positive.
        negative = bits < 0
        bits[negative] = floor - bits[negative]
        ordered.append(bits)
    return np.abs(ordered[0] - ordered[1])


def _flat(array: np.ndarray) -> np.ndarray:
    """One array as a contiguous vector, in the order the file stored it.

    Column-major is asked for explicitly rather than left to whichever
    layout the array happens to have, so that two arrays of the same shape
    are always walked in the same order.
    """
    return np.ascontiguousarray(np.asarray(array).reshape(-1, order="F"))


def compare_variable(ref: np.ndarray, got: np.ndarray, tol) -> dict:
    """One expected array against one submitted array.

    `tol` is the variable's {abs, rel, ulp} band for a floating-point
    variable, and is not consulted at all for any other type.
    """
    ref = np.asarray(ref)
    got = np.asarray(got)
    if ref.shape != got.shape:
        return {"pass": False, "error": f"shape {got.shape} != expected {ref.shape}"}
    if ref.dtype != got.dtype:
        return {"pass": False, "error": f"dtype {got.dtype.str} != expected {ref.dtype.str}"}

    ref = _flat(ref)
    got = _flat(got)
    if ref.dtype.kind != "f":
        # Integers and logicals are answers, not measurements: there is no
        # rounding for a band to allow for.
        bad = ref != got
        return {"pass": bool(not bad.any()), "n_bad": int(bad.sum()), "n": int(ref.size)}

    # The metrics are accumulated in double precision whatever the arrays
    # are, so that a large ratio between two single-precision numbers is a
    # number rather than an infinity.
    abs_err = np.abs(got.astype(np.float64) - ref.astype(np.float64))
    # The floor keeps the ratio defined where the expected value is exactly
    # zero. It is the smallest positive number of the arrays' own type, so
    # it never widens a comparison between values that type can represent.
    denominator = np.maximum(np.abs(ref.astype(np.float64)), np.finfo(ref.dtype).tiny)
    # Dividing a real error by the smallest double there is can overflow.
    # That is the answer -- the ratio is past anything a band would allow --
    # so it is taken rather than warned about.
    with np.errstate(over="ignore"):
        rel_err = abs_err / denominator
    ulp_err = _ulp_diff(ref, got)

    ok = (abs_err <= tol["abs"]) | (rel_err <= tol["rel"]) | (ulp_err <= tol["ulp"])
    return {
        "pass": bool(np.all(ok)),
        "max_abs": float(abs_err.max()),
        # An expected value of exactly zero leaves the ratio unbounded --
        # only the absolute band can pass such an element -- and the
        # verdict above has already been decided, so the number reported
        # here is capped to one a reader (and JSON) can hold.
        "max_rel": float(min(rel_err.max(), np.finfo(np.float64).max)),
        "max_ulp": int(ulp_err.max()),
        "n_bad": int((~ok).sum()),
        "n": int(ref.size),
    }


def compare_case(expected: dict, got: dict, tols: dict) -> dict:
    """Every variable the expected case holds, against what was submitted.

    A variable the expected case holds and the submission does not is a
    failure naming that variable -- never a silently skipped comparison. A
    variable the submission holds and the expected case does not is
    reported under "extra" and does not decide anything: the expected case
    is what defines the answer.

    A floating-point variable with no band in the tolerance policy raises
    rather than comparing: the oracle checks the policy against the code's
    declared outputs at startup, so reaching here means something got past
    that check.
    """
    per_var = {}
    for var, ref in expected.items():
        if var not in got:
            per_var[var] = {"pass": False, "error": f"variable '{var}' missing from the submitted outputs"}
            continue
        tol = tols[var] if np.asarray(ref).dtype.kind == "f" else None
        per_var[var] = compare_variable(ref, got[var], tol)
    return {
        "pass": all(v["pass"] for v in per_var.values()),
        "per_var": per_var,
        "extra": sorted(set(got) - set(expected)),
    }
