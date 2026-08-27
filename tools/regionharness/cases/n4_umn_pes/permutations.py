#!/usr/bin/env python3
"""Atom-relabelling symmetry of the N4 UMN PES, as an action on the 6 R slots.

WHICH PAIR IS R(k)?  Derived, not assumed.  Three independent pieces of
evidence in the tree, all agreeing on (1-2, 1-3, 1-4, 2-3, 2-4, 3-4):

  1. codes/CoarseAIR/src/PESs/PES_Class.F90:326-328 -- TransToCart_4Atoms, the
     routine that converts dV/dR into dV/dQ for exactly this PES, builds the
     component differences of the six pair vectors from the 12-long Cartesian
     vector Q (atom i occupies Q(3i-2:3i)):
         Rx = [Q(1)-Q(4), Q(1)-Q(7), Q(1)-Q(10), Q(4)-Q(7), Q(4)-Q(10), Q(7)-Q(10)]
     i.e. slot 1 is atoms (1,2), slot 2 (1,3), slot 3 (1,4), slot 4 (2,3),
     slot 5 (2,4), slot 6 (3,4).  This is the definitive statement: it is where
     R and the atom coordinates are related to each other.

  2. codes/CoarseAIR/src/PESs/N4_UMN_PES_Class.F90:245-250 -- the same object's
     Initialize builds the diatomic-potential table over the identical index
     list, iA(1..6,:) = [1,2], [1,3], [1,4], [2,3], [2,4], [3,4], and
     Compute_N4_UMN_PES_1d@347-350 sums Pairs(iP)%Vd over R(iP) with that
     table, so the diatomic term and the PIP term index R the same way.

  3. Internal corroboration from the fit itself.  EvMono
     (N4_UMN_PES_Class.F90:614-620) reverses the MEG terms,
     rm(1..6) = rms(6..1), and then forms exactly three products of two
     first-degree monomials:
         rm(7) = rm(3)*rm(4) = rms(4)*rms(3)
         rm(8) = rm(2)*rm(5) = rms(5)*rms(2)
         rm(9) = rm(1)*rm(6) = rms(6)*rms(1)
     Under the ordering above those are {2,3}x{1,4}, {2,4}x{1,3}, {3,4}x{1,2}
     -- the three perfect matchings of four atoms, the canonical A4 invariant
     basis.  Under any other ordering the three products would not partition
     the atoms.

THE ACTION.  For a permutation sigma of the four atoms, slot k = {a,b} maps to
the slot holding {sigma(a), sigma(b)}.  Writing pi = pi_sigma for that index
map, the relabelled geometry is

    R'[k] = R[pi[k]]

and because the PES is invariant under relabelling, V(R') = V(R) exactly (in
exact arithmetic) and the gradient carries the *same* index map:

    dVdR(R')[k] = dVdR(R)[pi[k]]

  (proof: g(R) := F(P R) with (P R)_k = R_{pi[k]}; invariance says g = F, so
   dF/dR_j (R) = dF/dx_{pi^-1(j)} (R'), i.e. grad(R')[k] = grad(R)[pi[k]].)

S4 acts faithfully on the six pairs, so the 24 sigmas give 24 distinct pi.

Run this file to re-measure the floating-point spread of that invariance
against the real driver; see main() for what it prints.
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import driver as drv                                        # noqa: E402

PAIR_ORDER = ((1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4))
SLOT_OF = {frozenset(p): k for k, p in enumerate(PAIR_ORDER)}
NATOMS = 4


def perm_index(sigma):
    """Index map pi on the 6 slots induced by an atom permutation.

    sigma is a tuple of length 4: atom i (1-based) is relabelled sigma[i-1].
    Returns pi with R_permuted[k] = R[pi[k]].
    """
    return tuple(SLOT_OF[frozenset((sigma[a - 1], sigma[b - 1]))]
                 for a, b in PAIR_ORDER)


def perms():
    """All 24 (sigma, pi) pairs, in a fixed order."""
    return [(s, perm_index(s))
            for s in itertools.permutations(range(1, NATOMS + 1))]


def apply_perm(pi, values):
    """values permuted by pi: out[k] = values[pi[k]]."""
    return [values[j] for j in pi]


# ---------------------------------------------------------------------------
# empirical validation
# ---------------------------------------------------------------------------


def load_corpus(path=None):
    """R rows from corpus.csv (igrad column dropped)."""
    import csv
    p = Path(path) if path else Path(__file__).resolve().parent / "corpus.csv"
    with open(p) as fh:
        rows = list(csv.DictReader(fh))
    return [[float(r["r%d" % (k + 1)]) for k in range(6)] for r in rows]


def validate(rows, workdir, verbose=True):
    """Run every pi on every row; return the measured discrepancies.

    Two scales are reported for the gradient. `g_rel` divides by the component
    itself, which is the honest per-component number but is dominated by
    components that are ~1e-8 while their neighbours are ~1e-4. `g_norm` divides
    by max(||dVdR||_inf, 1), which is the scale the property test actually needs:
    a mixed abs/rel bound.
    """
    P = perms()
    out = dict(v_rel=0.0, g_rel=0.0, g_norm=0.0,
               v_bits_bad=0, g_bits_bad=0, calls=0,
               v_cases=0, g_cases=0, worst_v=None, worst_g=None)
    for i, R in enumerate(rows):
        base = drv.run(Path(workdir) / ("row%02d_id" % i), R, 1)
        out["calls"] += 1
        gscale = max(max(abs(x) for x in base.dVdR), 1.0)
        for j, (sigma, pi) in enumerate(P):
            res = drv.run(Path(workdir) / ("row%02d_p%02d" % (i, j)),
                          apply_perm(pi, R), 1)
            out["calls"] += 1
            out["v_cases"] += 1
            rel = abs(res.V - base.V) / max(abs(base.V), 1.0)
            if res.V_hex != base.V_hex:
                out["v_bits_bad"] += 1
            if rel > out["v_rel"]:
                out["v_rel"], out["worst_v"] = rel, (i, sigma, pi, base.V, res.V)
            want = apply_perm(pi, base.dVdR)
            want_bits = apply_perm(pi, base.dVdR_hex)
            for k in range(6):
                out["g_cases"] += 1
                d = abs(res.dVdR[k] - want[k])
                if d / max(abs(want[k]), 1e-300) > out["g_rel"]:
                    out["g_rel"] = d / max(abs(want[k]), 1e-300)
                if d / gscale > out["g_norm"]:
                    out["g_norm"] = d / gscale
                    out["worst_g"] = (i, sigma, k, want[k], res.dVdR[k])
                if res.dVdR_hex[k] != want_bits[k]:
                    out["g_bits_bad"] += 1
        if verbose:
            print("  row %2d: V=%-22.17g  max rel V spread so far %.3e"
                  % (i, base.V, out["v_rel"]))
    if verbose and out["worst_v"]:
        i, sigma, pi, v0, v1 = out["worst_v"]
        print("  worst V   : row %d sigma=%s pi=%s  %.17g vs %.17g"
              % (i, sigma, pi, v0, v1))
        i, sigma, k, g0, g1 = out["worst_g"]
        print("  worst dVdR: row %d sigma=%s slot %d  %.17g vs %.17g"
              % (i, sigma, k + 1, g0, g1))
    return out


def main():
    import argparse
    import tempfile
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=5,
                    help="how many corpus rows to test (default 5)")
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()

    P = perms()
    assert len({pi for _, pi in P}) == 24, "S4 must act faithfully on the pairs"
    print("PAIR_ORDER = %s" % (PAIR_ORDER,))
    print("%d distinct slot permutations from %d atom permutations"
          % (len({pi for _, pi in P}), len(P)))

    rows = load_corpus()[: args.rows]
    tmp = args.workdir or tempfile.mkdtemp(prefix="n4pes_perm_")
    print("driver: %s\nworkdir: %s\nrows: %d" % (drv.DRIVER, tmp, len(rows)))
    m = validate(rows, tmp)
    print()
    print("calls                            : %d" % m["calls"])
    print("max relative V spread            : %.6e" % m["v_rel"])
    print("V bit-pattern mismatches         : %d / %d" % (m["v_bits_bad"], m["v_cases"]))
    print("max per-component dVdR rel spread: %.6e" % m["g_rel"])
    print("max dVdR spread / ||dVdR||_inf   : %.6e" % m["g_norm"])
    print("dVdR bit-pattern mismatches      : %d / %d" % (m["g_bits_bad"], m["g_cases"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
