"""The words a claim is written in, spelled once.

Trust role: a check writes a claim's detail, the gateway reads part of
it back to name the claim's materials, the status computation reads the
verdict, and the promotion command reads which sets a claim named. Each
of these is a different module in a different layer, and each has to
mean the same thing by the same word. A verdict word or a detail key
spelled at the point of use in six modules is six places one typo can
turn "the executables this claim names" into "this claim names no
executable", which reads as lost evidence rather than as a typo.

The ledger owns these words because the ledger is where they are
stored: every layer above it imports them from here. Nothing here
decides anything; it only says how the things a claim holds are called.
"""
from __future__ import annotations

# The two verdicts a claim can carry. A check that could not reach a
# verdict raises instead, and no claim is written.
PASS = "pass"
FAIL = "fail"

# Detail keys a layer other than the writer reads back.
#
# The executable a builder measured, as {"sha256": ...} with whatever
# else the builder reported about it. A build claim carries one per
# target under TARGETS_KEY; a run, capture, or timing claim carries the
# one it ran under EXECUTABLE_IDENTITY_KEY. The evidence layer collects
# every sha256 found under either into the claim's binary cohort.
EXECUTABLE_IDENTITY_KEY = "executable_identity"
TARGETS_KEY = "targets"
# The stored set a dataset entry points at, and the set of program
# outputs a timing run left behind for a port's own run to be compared
# against.
CAPTURE_SET_KEY = "capture_set"
PROGRAM_SET_KEY = "program_set"
# The digest of the tolerance policy a comparison was judged within.
POLICY_KEY = "policy_sha256"
# The digest of the original reference a harness was checked against.
REFERENCE_KEY = "reference_sha256"
