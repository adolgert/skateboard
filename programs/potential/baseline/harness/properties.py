"""Independent small-case checks of charge linearity and the equation."""
import numpy as np
from hypothesis import given, strategies as st
from harness_properties import run_replay, settings


@settings()
@given(n=st.integers(1, 19), shift=st.integers(-4, 4))
def test_equation_and_charge_linearity(n, shift):
    i = np.arange(n, dtype=np.float32)
    inputs = dict(x=i / 19, y=(i % 3) / 3, z=(i % 5) / 5,
                  q=(i % 7 - 3 + shift) / 7)
    distance2 = sum((inputs[k][:, None] - inputs[k][None, :])**2 for k in ('x','y','z'))
    expected = (inputs['q'][None, :] / np.sqrt(distance2 + 0.125)).sum(axis=1)
    actual = run_replay(inputs)['phi']
    np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-5)
    reversed_charge = run_replay({**inputs, 'q': -inputs['q']})['phi']
    np.testing.assert_allclose(reversed_charge, -actual, rtol=1e-5, atol=1e-5)
