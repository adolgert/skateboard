import harness_properties as harness
from hypothesis import given, strategies as st
import numpy as np

CORPUS = harness.corpus()


@st.composite
def states(draw):
    idx = draw(st.integers(0, len(CORPUS) - 1))
    case = CORPUS[idx]
    return {name: np.copy(arr) for name, arr in case.items()}


@harness.settings()
@given(states())
def test_determinism(state):
    """Calling the region twice on the same inputs produces identical outputs."""
    out1 = harness.run_replay(state)
    out2 = harness.run_replay(state)
    assert np.array_equal(out1["Q"], out2["Q"])
    assert out1["dt"] == out2["dt"]
