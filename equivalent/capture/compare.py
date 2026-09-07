"""Numerical comparator. Pure numpy over whatever arrays it is given.

Acceptance policy for a finite floating-point variable, per case: an element is
acceptable if ANY of its three metrics is within tolerance -- absolute,
relative, or units-in-last-place. Integer and logical variables carry no
tolerance at all: any differing element fails. A variable passes only if
EVERY element is acceptable, a case passes only if every variable passes,
and a dataset passes only if every case passes.

NaN and infinity are never accepted, including an identical pair. Scientific
codes sometimes use them deliberately, but accepting them requires a policy
that says where and why; this comparator's tolerance schema has no such field.
Rejecting them also keeps every recorded metric valid strict JSON.

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
from numbers import Integral, Real

import numpy as np

# Included in the oracle identity alongside this file's bytes. Bump when the
# meaning of a comparison changes even if packaging leaves the source bytes
# unchanged.
COMPARATOR_VERSION = "2"

# The unsigned integer type of the same width as each floating-point type.
# Unsigned arithmetic is essential for float64: the ordered distance from a
# negative value to a positive one can be larger than INT64_MAX.
ULP_UINT = {4: np.uint32, 8: np.uint64}


def _ulp_diff(ref: np.ndarray, got: np.ndarray) -> np.ndarray:
    """Distance in representable steps of the arrays' own type, across sign changes."""
    try:
        as_uint = ULP_UINT[ref.dtype.itemsize]
    except KeyError as exc:
        raise ValueError(f"ULP distance is unsupported for dtype {ref.dtype}") from exc
    native = ref.dtype.newbyteorder("=")
    sign = as_uint(1 << (ref.dtype.itemsize * 8 - 1))
    ordered = []
    for array in (ref, got):
        bits = array.astype(native, copy=False).view(as_uint)
        # Map IEEE sign-magnitude bits to monotonically ordered unsigned
        # integers. Negative values are complemented; nonnegative values
        # have the sign bit set. Both branches retain the original width.
        mapped = np.where((bits & sign) != 0, ~bits, bits | sign)
        ordered.append(mapped.astype(np.uint64))
    high = np.maximum(ordered[0], ordered[1])
    low = np.minimum(ordered[0], ordered[1])
    return high - low


def _flat(array: np.ndarray) -> np.ndarray:
    """One array as a contiguous vector, in the order the file stored it.

    Column-major is asked for explicitly rather than left to whichever
    layout the array happens to have, so that two arrays of the same shape
    are always walked in the same order.
    """
    return np.ascontiguousarray(np.asarray(array).reshape(-1, order="F"))


def tolerance_problem(tol) -> str | None:
    if not isinstance(tol, dict):
        return "floating-point tolerance is not a mapping"
    for name in ("abs", "rel"):
        value = tol.get(name)
        if isinstance(value, bool) or not isinstance(value, Real):
            return f"tolerance '{name}' is not a real number"
        if not np.isfinite(value) or value < 0:
            return f"tolerance '{name}' must be finite and nonnegative"
    ulp = tol.get("ulp")
    if isinstance(ulp, bool) or not isinstance(ulp, Integral) or ulp < 0:
        return "tolerance 'ulp' must be a nonnegative integer"
    return None


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

    problem = tolerance_problem(tol)
    if problem:
        return {
            "pass": False, "error": problem,
            "n_bad": int(ref.size), "n": int(ref.size),
        }

    finite = np.isfinite(ref) & np.isfinite(got)
    n_nonfinite = int((~finite).sum())
    if not ref.size:
        return {
            "pass": True, "max_abs": 0.0, "max_rel": 0.0, "max_ulp": 0,
            "n_bad": 0, "n": 0, "n_nonfinite": 0,
        }

    # The metrics are accumulated in double precision whatever the arrays
    # are, so that a large ratio between two single-precision numbers is a
    # number rather than an infinity.
    finite_ref = ref[finite]
    finite_got = got[finite]
    with np.errstate(over="ignore"):
        abs_err = np.abs(finite_got.astype(np.float64) - finite_ref.astype(np.float64))
    # The floor keeps the ratio defined where the expected value is exactly
    # zero. It is the smallest positive number of the arrays' own type, so
    # it never widens a comparison between values that type can represent.
    denominator = np.maximum(np.abs(finite_ref.astype(np.float64)), np.finfo(ref.dtype).tiny)
    # Dividing a real error by the smallest double there is can overflow.
    # That is the answer -- the ratio is past anything a band would allow --
    # so it is taken rather than warned about.
    with np.errstate(over="ignore"):
        rel_err = abs_err / denominator
    ulp_err = _ulp_diff(finite_ref, finite_got)

    ok = (abs_err <= tol["abs"]) | (rel_err <= tol["rel"]) | (ulp_err <= tol["ulp"])
    result = {
        "pass": bool(not n_nonfinite and np.all(ok)),
        "max_abs": float(min(abs_err.max(initial=0.0), np.finfo(np.float64).max)),
        # An expected value of exactly zero leaves the ratio unbounded --
        # only the absolute band can pass such an element -- and the
        # verdict above has already been decided, so the number reported
        # here is capped to one a reader (and JSON) can hold.
        "max_rel": float(min(rel_err.max(initial=0.0), np.finfo(np.float64).max)),
        "max_ulp": int(ulp_err.max(initial=0)),
        "n_bad": int((~ok).sum()) + n_nonfinite,
        "n": int(ref.size),
        "n_nonfinite": n_nonfinite,
    }
    if n_nonfinite:
        result["error"] = (
            f"{n_nonfinite} element(s) contain NaN or infinity; the comparison policy "
            "requires finite floating-point outputs"
        )
    return result


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
