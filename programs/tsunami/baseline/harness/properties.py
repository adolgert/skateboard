"""Invariants of this code's region, checked by searching for inputs that break them.

Trust role: this file states what must be true of `step` for any input,
and a port that breaks one of them is refused. The capture-replay checks
around it compare a port against recorded answers, which says a port is
right on the inputs someone happened to capture; these say what the code
is supposed to do. So a property written too loosely here -- a tolerance
wide enough to swallow a real error -- makes a port look searched when it
was not. A property written too tightly is the safer mistake: it fails,
and a person reads the failing example.

It runs inside the builder, against the replay binary built from the
submitted tree. `harness_properties` is the builder's own library, not
part of this code: it turns "run the region on these arrays" into an
invocation of that binary. Nothing here names a path, a binary, or a
seed.

The inputs are drawn by perturbing the visible cases rather than being
invented from nothing. A shallow-water state is not any pair of arrays:
h is a water height near the mean depth and u is a velocity that the
time step is stable for. Scaling a captured state by a few per cent keeps
the drawn state one the code is meant to be run on, so a failure is about
the port and not about an input the baseline could not handle either.

Why the third property holds bitwise rather than within a band: the
stencil in mod_diff is periodic, and both updates in mod_kernel are
elementwise over the whole array. Rotating the grid therefore rotates
which element each arithmetic operation lands on without changing the
operands of any of them, so every result is the same floating-point
number in a different place. A port that tiles the loop and treats the
wrap-around specially is exactly what this catches.
"""
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as npst

import numpy as np

import harness_properties as harness

# The captured states every drawn input is a perturbation of, and how
# many grid points they hold -- read from the data rather than written
# down, so a dataset captured at another size still runs.
CORPUS = harness.corpus()
GRID = len(CORPUS[0]["h"])

# How far a drawn state may sit from a captured one, per element. Ten per
# cent is far enough to be a different state and near enough to still be
# a shallow-water one: the code's own time step is stable for depths
# around the mean, not for arbitrary ones.
SPREAD = 0.1
# The same two numbers as float32 sees them. A bound that is not itself a
# float32 has no meaning for a float32 draw, and Hypothesis says so rather
# than rounding it quietly.
LOW = float(np.float32(1.0 - SPREAD))
HIGH = float(np.float32(1.0 + SPREAD))

# How much the total water may change in one step. In exact arithmetic it
# may not change at all: the continuity update subtracts a centered
# difference of the mass flux, and a centered difference over a periodic
# grid sums to zero. What is left is float32 rounding over the hundred
# elements of the sum. Measured across 400 drawn states against the
# gfortran -O2 baseline, the worst relative change was 1.5e-08; the bound
# here is eight times that, which is the same margin the code's tolerance
# file uses over its own measurements.
MASS_REL = 1.2e-07


@st.composite
def states(draw):
    """One captured case with every element of h and u scaled a little."""
    case = CORPUS[draw(st.integers(0, len(CORPUS) - 1))]
    scaled = {}
    for name, array in case.items():
        factors = draw(npst.arrays(
            dtype=array.dtype, shape=array.shape,
            elements=st.floats(
                min_value=LOW, max_value=HIGH,
                allow_nan=False, allow_infinity=False, width=32,
            ),
        ))
        scaled[name] = (array * factors).astype(array.dtype)
    return scaled


def _same_bits(first, second) -> bool:
    """Identical bit patterns, which is stricter than == and says so for NaN."""
    return first.shape == second.shape and first.tobytes() == second.tobytes()


@harness.settings()
@given(states())
def test_one_step_is_the_same_step_twice(state):
    """The region is a function of its inputs and nothing else.

    A port whose answer depends on how the work happened to be scheduled
    fails here, and it is the one failure that says nothing about physics.
    """
    first = harness.run_replay(state)
    second = harness.run_replay(state)

    assert sorted(first) == sorted(second)
    for name in first:
        assert _same_bits(first[name], second[name]), f"'{name}' differed between two runs"


@harness.settings()
@given(states())
def test_one_step_conserves_the_total_water(state):
    """No water is created or destroyed by the continuity update.

    What this catches is a stencil that has stopped being a periodic
    centered difference -- a first or last element that no longer wraps
    around, an element written twice or not at all. Such a difference no
    longer sums to zero, and the water lost at the seam accumulates every
    step. Measured with the wrap-around removed from the first element,
    one step moved 7.7e-04 out of a total of 12.5: six thousand times the
    bound.

    It does not catch a change to the flux being differenced. The sum of
    a periodic centered difference is zero whatever it is a difference
    of, so a port that computed the wrong mass flux would still conserve
    the total. That failure belongs to the regression checks, which
    compare against what the baseline actually computed.
    """
    before = np.sum(state["h"], dtype=np.float64)
    after = np.sum(harness.run_replay(state)["h"], dtype=np.float64)

    assert abs(after - before) <= MASS_REL * abs(before), (
        f"the total water changed by {after - before:.6e} from {before:.6e}"
    )


@harness.settings()
@given(states(), st.integers(min_value=1, max_value=GRID - 1))
def test_a_rotated_grid_gives_a_rotated_answer(state, shift):
    """The grid has no privileged position, so moving the water moves the answer.

    Rotating a periodic grid is a relabelling of its points. Every
    arithmetic operation keeps the same operands, so the answer is the
    same numbers in rotated places -- bitwise, not within a band.
    """
    rotated_first = harness.run_replay({
        name: np.roll(array, shift) for name, array in state.items()
    })
    then_rotated = {
        name: np.roll(array, shift) for name, array in harness.run_replay(state).items()
    }

    for name in then_rotated:
        assert _same_bits(then_rotated[name], rotated_first[name]), (
            f"'{name}' after rotating by {shift} is not the rotation of '{name}'"
        )
