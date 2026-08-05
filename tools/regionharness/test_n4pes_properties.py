#!/usr/bin/env python3
"""Property-based tests for the n4pes region, over the plain-file driver.

Capture-replay proves the region reproduces 50 recorded points bitwise. That is
a strong statement about those 50 points and no statement at all about any
other. This layer states what is supposed to be true *for all* inputs in the
region's operating envelope, and lets Hypothesis look for a counterexample:

  a. permutation invariance   -- the PES is a permutation-invariant polynomial
                                 in the six pair distances of four identical N
                                 atoms, so relabelling the atoms must not move
                                 V, and must carry dVdR along by the same index
                                 map.  This is the property a GPU port is most
                                 likely to break, because it is the property the
                                 fit's basis construction encodes.
  b. gradient consistency     -- dVdR must be the gradient of V.  V and dVdR
                                 come from two separate code paths (EvV vs
                                 EvdVdR, with two separate 300+ line polynomial
                                 evaluations), so this is a real cross-check,
                                 not a tautology.
  c. igrad consistency        -- V must not depend on whether the gradient was
                                 also requested.  igrad=1 runs strictly more
                                 code over the same shared scratch arrays.
  d. determinism              -- same bytes in, same bytes out.

Every evaluation is a fresh process (see cases/n4_umn_pes/driver.py for why).

Tolerances are measured, not guessed; the numbers below are recorded with the
measurement that produced them, and `--calibrate` re-runs the measurement.

Usage:
  N4PES_MAX_EXAMPLES=50 .venv/bin/pytest tools/regionharness/test_n4pes_properties.py -v
  N4PES_SEED=<n> ...                     reproduce a previous run
  .venv/bin/python tools/regionharness/test_n4pes_properties.py --calibrate
"""
import csv
import itertools
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

HERE = Path(__file__).resolve().parent
CASE = HERE / "cases" / "n4_umn_pes"
sys.path.insert(0, str(CASE))
import driver as drv                                        # noqa: E402
import permutations as perm                                 # noqa: E402

# ---------------------------------------------------------------------------
# measured tolerances
# ---------------------------------------------------------------------------
#
# Permutation invariance, measured by cases/n4_umn_pes/permutations.py over all
# 50 corpus rows x all 24 permutations (1250 driver calls, 2026-08-05):
#
#     max |dV| / max(|V|,1)                   2.298e-13   (V is NOT bitwise
#                                                          invariant: 244/1200
#                                                          images differ in bits)
#     max |d(dVdR)| / max(||dVdR||_inf,1)     8.807e-13
#     max per-component |d(dVdR)|/|dVdR_k|    3.413e-11   (dominated by
#                                                          components ~1e-11
#                                                          sitting next to
#                                                          components ~1e-4)
#
# So the invariance is a floating-point statement, not a bitwise one: the fit's
# basis is symmetric but the *summation order* over the 276 basis functions is
# not, and reassociation of a ~1e8-magnitude cancelling sum shows up at 1e-13
# relative.  Tolerances take ~10x headroom on the measured spread.
PERM_V_RTOL = 2.5e-12          # relative to max(|V|, 1)
PERM_G_TOL = 1.0e-11           # relative to max(||dVdR||_inf, 1) -- mixed abs/rel

# Central-difference gradient, h = FD_H_REL * max(|R_k|, 1), compared against
# dVdR with
#
#     |fd - g|  <=  FD_ATOL  +  FD_RTOL*|g|  +  FD_VNOISE*|V| / (2h)
#
# The third term is the point, and getting there took two test failures worth
# recording.
#
# Attempt 1 -- h = 1e-6*|R|, tolerance calibrated on the 50 corpus points
# (frontier |fd-g| <= 5.154e-06 + 1e-6*|g|; in force 5e-05 + 1e-6*|g|).  A
# 1000-example run shrank to
#     R = [1.043548710928332, 1.75, 1.625, 2.5, 2.421875, 1.04296875]
#     dVdR(6) = 14.679679043295437  fd = 14.679595585633418  diff = 8.35e-05
# 1.3x over.  Not a gradient bug: sweeping h at that point walks the difference
# through a noise floor and back out,
#     h*|R|    1e-3      1e-4      1e-5      1e-6      1e-7      1e-8     1e-9
#     |fd-g| 2.3e-04   1.7e-06   1.0e-06   8.3e-05   1.3e-04   9.1e-03  9.6e-02
# -- truncation on the left, cancellation on the right, optimum near 1e-5..1e-4.
# The corpus hid it because every captured point is a nearly dissociated
# geometry (V ~ 457.4 = totdiss, most |dVdR| ~ 1e-05); Hypothesis goes straight
# for the compressed corner of the envelope, where V ~ 1e+03 and |dVdR| ~ 6e+02.
#
# Attempt 2 -- h = 1e-5*|R|, |fd-g| <= 1e-7 + 1e-5*|g|, calibrated on 130 points
# (corpus + strategy draws).  A 5000-example run shrank to
#     R = [1.265625, 1.462890625, 1.375, 2.71875, 2.71875, 1.259765625]
#     dVdR(5) = 0.053653202712006021  fd = 0.053653925481311239  diff = 7.23e-07
# 1.14x over.  Also not a gradient bug -- and the pattern is now clear.  Neither
# an absolute nor a |g|-relative term can track this error, because the error is
# not a property of g at all: it is the roundoff noise of V, amplified by 1/2h.
#
# So model it.  Central-difference cancellation error is eps_V*|V|/(2h) where
# eps_V is V's own relative noise.  `--calibrate` solves for eps_V over 1500
# components (50 corpus + 80 uniform strategy draws + 120 endpoint-biased draws,
# the corner Hypothesis actually hunts) and reports, per step size:
#
#     h/max|R|   max |fd-g|   eps_V p50    eps_V p99   eps_V max   err/tol
#     1e-4        4.271e-05   2.064e-16    6.845e-12   1.395e-11    1.018
#     3e-5        8.233e-06   1.523e-16    2.578e-13   6.275e-13    0.052
#     1e-5        1.817e-05   1.315e-16    1.975e-13   5.020e-13    0.063
#     1e-6        2.692e-04   1.280e-16    2.305e-13   6.725e-13    0.104
#
# The median implied eps_V is one ulp -- for most geometries V is as clean as a
# single operation.  The tail is not: near a compressed geometry V carries a few
# thousand ulp, which is what a 276-term sum with 1e+05 coefficients cancelling
# down to ~1e+03 should carry.  FD_VNOISE = 5e-12 is 8x the worst of those.
#
# h = 3e-5*max(|R_k|,1) is the optimum, and err/tol = 0.052 there -- 19x
# headroom, with a median tolerance of ~8e-06, roughly 50x sharper than the flat
# atol attempt 1 would have needed.  (The 1e-4 row exceeds 1.0 because at that
# step truncation, which this model does not carry, has taken over: eps_V is no
# longer noise, it is V''' h^2 in disguise.)
#
# DEVIATION: the Phase-5 spec asked for h = 1e-6*max(|R_k|,1) and a mixed
# abs/rel tolerance.  The h and the extra term are the measurements above, not a
# preference.  Override h with N4PES_FD_H.
#
# Standing caveat: this difference cannot resolve gradient components below
# ~1e-6, and 210 of the 300 corpus components are below 1e-4.  For those the
# check says only "not wildly wrong".
FD_H_REL = float(os.environ.get("N4PES_FD_H", "3e-5"))
FD_ATOL = 1.0e-7
FD_RTOL = 1.0e-6
FD_VNOISE = 5.0e-12            # relative roundoff noise of V, amplified by 1/2h

# ---------------------------------------------------------------------------
# run configuration
# ---------------------------------------------------------------------------

MAX_EXAMPLES = int(os.environ.get("N4PES_MAX_EXAMPLES", "50"))

# derandomize is deliberately NOT set: each run should explore somewhere new.
# But an unreproducible failure is nearly useless, so the run draws one seed,
# prints it, and pins every test to it.  N4PES_SEED=<n> replays that run.
RUN_SEED = int(os.environ.get("N4PES_SEED") or random.SystemRandom().getrandbits(32))

SCRATCH = Path(os.environ.get("N4PES_SCRATCH")
               or tempfile.mkdtemp(prefix="n4pes_props_"))
_counter = itertools.count()

print("n4pes properties: seed=%d max_examples=%d scratch=%s"
      % (RUN_SEED, MAX_EXAMPLES, SCRATCH), file=sys.stderr)


KEEP_CASES = bool(os.environ.get("N4PES_KEEP"))


def case_dir(tag):
    """A fresh directory for one driver invocation."""
    d = SCRATCH / ("%s_%06d" % (tag, next(_counter)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def evaluate(tag, R, igrad):
    """One region evaluation in a throwaway directory.

    The directory is removed on success and kept on failure -- a run of a few
    thousand examples is ~100k invocations, which is a lot of inodes to leave
    behind, but the one case that broke is worth having on disk.
    N4PES_KEEP=1 keeps them all.
    """
    d = case_dir(tag)
    res = drv.run(d, R, igrad)
    if not KEEP_CASES:
        shutil.rmtree(d, ignore_errors=True)
    return res


def props(**kw):
    """The settings every property here shares."""
    opts = dict(max_examples=MAX_EXAMPLES, deadline=None,
                suppress_health_check=[HealthCheck.too_slow])
    opts.update(kw)
    return settings(**opts)


# ---------------------------------------------------------------------------
# corpus-derived strategy
# ---------------------------------------------------------------------------

CORPUS_CSV = CASE / "corpus.csv"
NR = drv.NR


def load_corpus():
    if not CORPUS_CSV.is_file():
        pytest.skip("no corpus: run tools/regionharness/export_corpus.py")
    with open(CORPUS_CSV) as fh:
        rows = list(csv.DictReader(fh))
    return [[float(r["r%d" % (k + 1)]) for k in range(NR)] for r in rows]


CORPUS = load_corpus()
# Per-component envelope, widened 10% past the observed range.  Six arbitrary
# "plausible" distances are almost never realizable as four points in R^3
# (Cayley-Menger), and a PES extrapolated to a nonexistent geometry says nothing
# about the code under test.  Perturbing a real captured geometry component-wise
# keeps the sample near the manifold the fit was trained on.
ENV_LO = [0.9 * min(row[k] for row in CORPUS) for k in range(NR)]
ENV_HI = [1.1 * max(row[k] for row in CORPUS) for k in range(NR)]
SPREAD = 0.10                  # +/-10% multiplicative perturbation


@st.composite
def r_vectors(draw, spread=SPREAD):
    """A corpus row with each component multiplicatively perturbed."""
    base = CORPUS[draw(st.integers(0, len(CORPUS) - 1))]
    out = []
    for k in range(NR):
        lo = max(ENV_LO[k], base[k] * (1.0 - spread))
        hi = min(ENV_HI[k], base[k] * (1.0 + spread))
        if not lo <= hi:                       # envelope narrower than the spread
            lo = hi = min(max(base[k], ENV_LO[k]), ENV_HI[k])
        out.append(draw(st.floats(min_value=lo, max_value=hi,
                                  allow_nan=False, allow_infinity=False)))
    return out


PERMS = perm.perms()


# ---------------------------------------------------------------------------
# a. permutation invariance
# ---------------------------------------------------------------------------


@props()
@seed(RUN_SEED)
@given(R=r_vectors(), j=st.integers(0, len(PERMS) - 1))
def test_permutation_invariance_of_V(R, j):
    sigma, pi = PERMS[j]
    base = evaluate("permV_a", R, 1)
    image = evaluate("permV_b", perm.apply_perm(pi, R), 1)
    tol = PERM_V_RTOL * max(abs(base.V), 1.0)
    assert abs(image.V - base.V) <= tol, (
        "V not invariant under atom relabelling sigma=%s (slot map %s):\n"
        "  R      = %r\n  V(R)   = %.17g  [%s]\n  V(piR) = %.17g  [%s]\n"
        "  |diff| = %.3e   tol = %.3e"
        % (sigma, pi, R, base.V, base.V_hex, image.V, image.V_hex,
           abs(image.V - base.V), tol))


@props()
@seed(RUN_SEED)
@given(R=r_vectors(), j=st.integers(0, len(PERMS) - 1))
def test_permutation_covariance_of_dVdR(R, j):
    sigma, pi = PERMS[j]
    base = evaluate("permG_a", R, 1)
    image = evaluate("permG_b", perm.apply_perm(pi, R), 1)
    want = perm.apply_perm(pi, base.dVdR)
    tol = PERM_G_TOL * max(max(abs(x) for x in base.dVdR), 1.0)
    for k in range(NR):
        assert abs(image.dVdR[k] - want[k]) <= tol, (
            "dVdR does not transform as the slot map under sigma=%s (%s):\n"
            "  R        = %r\n  slot     = %d\n"
            "  expected = %.17g\n  got      = %.17g\n  |diff|   = %.3e  tol = %.3e"
            % (sigma, pi, R, k + 1, want[k], image.dVdR[k],
               abs(image.dVdR[k] - want[k]), tol))


# ---------------------------------------------------------------------------
# b. finite-difference gradient
# ---------------------------------------------------------------------------


def fd_component(R, k, tag, hrel=None):
    """Central difference dV/dR_k, using the igrad=0 (value-only) path.

    Returns (fd, h) -- h is needed to size the cancellation term of the tolerance.
    """
    h = (hrel if hrel is not None else FD_H_REL) * max(abs(R[k]), 1.0)
    plus, minus = list(R), list(R)
    plus[k] += h
    minus[k] -= h
    vp = evaluate(tag + "_p", plus, 0).V
    vm = evaluate(tag + "_m", minus, 0).V
    return (vp - vm) / (2.0 * h), h


def fd_tolerance(g, V, h):
    """Absolute tolerance for |fd - g|; see the FD_* block for the model."""
    return FD_ATOL + FD_RTOL * abs(g) + FD_VNOISE * abs(V) / (2.0 * h)


@props()
@seed(RUN_SEED)
@given(R=r_vectors())
def test_gradient_matches_finite_difference(R):
    base = evaluate("fd_base", R, 1)
    for k in range(NR):
        fd, h = fd_component(R, k, "fd%d" % k)
        g = base.dVdR[k]
        tol = fd_tolerance(g, base.V, h)
        assert abs(fd - g) <= tol, (
            "dVdR(%d) is not the derivative of V:\n"
            "  R      = %r\n  V      = %.17g\n  dVdR   = %.17g\n  fd     = %.17g\n"
            "  |diff| = %.3e   tol = %.3e   (implied eps_V = %.3e)"
            % (k + 1, R, base.V, g, fd, abs(fd - g), tol,
               abs(fd - g) * 2.0 * h / max(abs(base.V), 1e-300)))


# ---------------------------------------------------------------------------
# c. igrad consistency
# ---------------------------------------------------------------------------


@props()
@seed(RUN_SEED)
@given(R=r_vectors())
def test_V_independent_of_igrad(R):
    v0 = evaluate("ig0", R, 0)
    v1 = evaluate("ig1", R, 1)
    assert v0.V_hex == v1.V_hex, (
        "V differs between igrad=0 and igrad=1:\n"
        "  R        = %r\n  igrad=0  = %.17g [%s]\n  igrad=1  = %.17g [%s]"
        % (R, v0.V, v0.V_hex, v1.V, v1.V_hex))
    assert "dVdR(1)" not in v0.values, "igrad=0 must not report dVdR"
    assert "dVdR(1)" in v1.values, "igrad=1 must report dVdR"


# ---------------------------------------------------------------------------
# d. determinism
# ---------------------------------------------------------------------------


@props()
@seed(RUN_SEED)
@given(R=r_vectors(), igrad=st.sampled_from([0, 1]))
def test_repeated_evaluation_is_byte_identical(R, igrad):
    d = case_dir("det")
    first = drv.run(d, R, igrad)
    second = drv.run(d, R, igrad)          # same input.txt, second process
    same = first.text == second.text
    if same and not KEEP_CASES:
        shutil.rmtree(d, ignore_errors=True)
    assert same, (
        "driver is not deterministic for R=%r igrad=%d:\n---\n%s---\n%s---"
        % (R, igrad, first.text, second.text))


# ---------------------------------------------------------------------------
# calibration (not a test): re-measure the tolerances above
# ---------------------------------------------------------------------------


def calibration_sample(rng_seed=999):
    """Corpus + uniform strategy draws + endpoint-biased draws.

    The corpus alone is not representative of what the strategy reaches: every
    captured point is a nearly dissociated geometry, and calibrating only there
    underestimates the finite-difference noise by an order of magnitude (twice,
    see the FD_* block).  Hypothesis prefers interval endpoints, so the third
    group draws components at the edge of the perturbation window.
    """
    rnd = random.Random(rng_seed)
    sample = list(CORPUS)
    for _ in range(80):
        b = rnd.choice(CORPUS)
        sample.append([min(max(b[k] * rnd.uniform(1 - SPREAD, 1 + SPREAD),
                               ENV_LO[k]), ENV_HI[k]) for k in range(NR)])
    edges = [1 - SPREAD, 1 - SPREAD, 1 + SPREAD, None]
    for _ in range(120):
        b = rnd.choice(CORPUS)
        row = []
        for k in range(NR):
            f = rnd.choice(edges)
            if f is None:
                f = rnd.uniform(1 - SPREAD, 1 + SPREAD)
            row.append(min(max(b[k] * f, ENV_LO[k]), ENV_HI[k]))
        sample.append(row)
    return sample


def calibrate(workdir):
    sample = calibration_sample()
    print("finite-difference calibration: %d points (%d corpus + 200 sampled),"
          " %d components each" % (len(sample), len(CORPUS), NR))
    print()
    print("%-9s %12s %12s %12s %12s %10s"
          % ("h/max|R|", "max |fd-g|", "eps_V p50", "eps_V p99", "eps_V max",
             "err/tol"))
    for hrel in (1e-4, 3e-5, 1e-5, 1e-6):
        recs = []
        for i, R in enumerate(sample):
            base = evaluate("cal%03d" % i, R, 1)
            for k in range(NR):
                fd, h = fd_component(R, k, "cal%03d_%d" % (i, k), hrel=hrel)
                g = base.dVdR[k]
                e = abs(fd - g)
                recs.append((e, e * 2.0 * h / max(abs(base.V), 1e-300),
                             e / fd_tolerance(g, base.V, h)))
        eps = sorted(r[1] for r in recs)
        print("%-9.0e %12.3e %12.3e %12.3e %12.3e %10.3f"
              % (hrel, max(r[0] for r in recs), eps[len(eps) // 2],
                 eps[int(len(eps) * 0.99)], eps[-1], max(r[2] for r in recs)))
    print()
    print("  err/tol is against the constants in force; < 1 passes, and its")
    print("  reciprocal is the headroom.  eps_V is the relative noise of V")
    print("  implied by |fd-g|*2h/|V|; FD_VNOISE should be ~10x its maximum.")
    print("  in force: FD_H_REL=%g  FD_ATOL=%g  FD_RTOL=%g  FD_VNOISE=%g"
          % (FD_H_REL, FD_ATOL, FD_RTOL, FD_VNOISE))
    print()
    print("permutation invariance: run")
    print("  .venv/bin/python %s --rows 50" % (CASE / "permutations.py"))
    print("  in force: PERM_V_RTOL = %g  PERM_G_TOL = %g" % (PERM_V_RTOL, PERM_G_TOL))


if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        calibrate(SCRATCH / "calibrate")
        sys.exit(0)
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
