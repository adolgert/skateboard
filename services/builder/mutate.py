"""Mutation result classification for the builder service.

The source-level operator table and generator live in mutation_source so the
builder and standalone fmutate command use one implementation.
"""
from __future__ import annotations

try:
    from .compare import compare_variable
except ImportError:  # pragma: no cover - exercised by whichever layout runs
    from equivalent.capture.compare import compare_variable

from .mutation_source import (
    BUILD_FAIL,
    EQUIVALENT,
    GAP,
    KEEP_DIRECTORY,
    KILLED,
    PENDING,
    RUNTIME_FAIL,
    SKIPPED,
    STATUSES,
    Mutant,
    generate,
)


def bitwise_equal(expected, got) -> bool:
    """Identical bit patterns, whatever the element type is.

    Comparing the bytes rather than the values is what makes two NaNs
    equal and two zeros of opposite sign different, which is what "the
    mutant changed the answer at all" has to mean.
    """
    return (
        expected.shape == got.shape
        and expected.dtype == got.dtype
        and expected.tobytes() == got.tobytes()
    )


def classify(expected: dict, got: dict, bands: dict) -> tuple[str, str]:
    """(status, note) for one mutant, from the captured answers and the bands.

    `expected` and `got` are {variable: array}; `bands` is the policy's
    band per variable, consulted for floating-point variables only, the
    way the comparator consults it.

    Every output identical bit for bit is a survivor. Otherwise the
    comparator decides: an output outside its band is the harness
    noticing, and an output that changed inside every band is the gap the
    policy is hiding. A variable the mutant did not write at all is the
    harness noticing too -- there is no answer to compare.
    """
    changed = []
    outside = []
    for variable in sorted(expected):
        if variable not in got:
            return KILLED, f"the replay wrote no '{variable}'"
        if bitwise_equal(expected[variable], got[variable]):
            continue
        changed.append(variable)
        band = bands.get(variable) if expected[variable].dtype.kind == "f" else None
        if not compare_variable(expected[variable], got[variable], band)["pass"]:
            outside.append(variable)

    if not changed:
        return EQUIVALENT, "no output changed"
    if outside:
        return KILLED, f"outside the band: {', '.join(outside)}"
    return GAP, f"changed within the band: {', '.join(changed)}"
