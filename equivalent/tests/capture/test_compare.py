"""How the harness decides whether two arrays are the same answer.

There is one comparator, asked by the oracle about a replay's outputs and
by the gateway about a ported program's whole-program outputs, so these
read as the statement of what "close enough" means everywhere: floating-
point variables pass on any one of three metrics under the code's own
tolerances, and integer and logical variables have to match exactly.
"""
from __future__ import annotations

import numpy as np
import pytest

from equivalent.capture import compare

# What a code's tolerances.json holds for one floating-point variable.
LOOSE = {"abs": 1e-6, "rel": 1e-5, "ulp": 16}
BITWISE = {"abs": 0.0, "rel": 0.0, "ulp": 0}
ONE_ULP = {"abs": 0.0, "rel": 0.0, "ulp": 1}


def _next_after(array):
    """The same array with every element moved one representable step up."""
    return np.nextafter(array, np.inf).astype(array.dtype)


def test_an_identical_float_array_passes_with_no_room_at_all():
    ref = np.array([1.0, 2.0, 3.0], dtype="<f8")

    result = compare.compare_variable(ref, ref.copy(), BITWISE)

    assert result["pass"] is True
    assert result["max_ulp"] == 0
    assert result["n"] == 3


def test_a_one_ulp_float64_difference_passes_under_ulp_one():
    ref = np.array([1.0, 2.0, 3.0], dtype="<f8")

    result = compare.compare_variable(ref, _next_after(ref), ONE_ULP)

    assert result["pass"] is True
    assert result["max_ulp"] == 1


def test_the_same_one_ulp_difference_fails_under_ulp_zero():
    ref = np.array([1.0, 2.0, 3.0], dtype="<f8")

    result = compare.compare_variable(ref, _next_after(ref), BITWISE)

    assert result["pass"] is False
    assert result["max_ulp"] == 1
    assert result["n_bad"] == 3


def test_a_one_ulp_float32_difference_is_measured_in_float32_steps():
    ref = np.array([1.0, 2.0, 3.0], dtype="<f4")

    result = compare.compare_variable(ref, _next_after(ref), ONE_ULP)

    assert result["pass"] is True
    assert result["max_ulp"] == 1


def test_a_relative_error_inside_the_band_passes_even_when_the_absolute_one_does_not():
    ref = np.array([1.0e6], dtype="<f8")
    got = np.array([1.0e6 + 1.0], dtype="<f8")

    result = compare.compare_variable(ref, got, {"abs": 1e-9, "rel": 1e-5, "ulp": 0})

    assert result["pass"] is True
    assert result["max_abs"] == 1.0


def test_a_column_major_two_dimensional_array_compares_element_for_element():
    ref = np.arange(6, dtype="<f8").reshape((2, 3), order="F")
    got = ref.copy(order="F")
    got[1, 2] += 1.0

    result = compare.compare_variable(ref, got, BITWISE)

    assert result["pass"] is False
    assert result["n_bad"] == 1
    assert result["n"] == 6


def test_an_integer_output_with_one_differing_element_fails():
    ref = np.array([1, 2, 3, 4], dtype="<i4")
    got = np.array([1, 2, 9, 4], dtype="<i4")

    result = compare.compare_variable(ref, got, None)

    assert result["pass"] is False
    assert result["n_bad"] == 1


def test_an_identical_integer_output_passes_with_no_tolerance_at_all():
    ref = np.array([1, 2, 3], dtype="<i8")

    assert compare.compare_variable(ref, ref.copy(), None)["pass"] is True


def test_a_logical_output_is_compared_exactly():
    ref = np.array([True, False, True], dtype="|b1")
    got = np.array([True, True, True], dtype="|b1")

    result = compare.compare_variable(ref, got, None)

    assert result["pass"] is False
    assert result["n_bad"] == 1


def test_a_shape_mismatch_fails_and_names_both_shapes():
    ref = np.zeros((2, 3), dtype="<f8")
    got = np.zeros((3, 2), dtype="<f8")

    result = compare.compare_variable(ref, got, LOOSE)

    assert result["pass"] is False
    assert "(3, 2)" in result["error"] and "(2, 3)" in result["error"]


def test_a_dtype_mismatch_fails_and_names_both_types():
    ref = np.zeros(3, dtype="<f8")
    got = np.zeros(3, dtype="<f4")

    result = compare.compare_variable(ref, got, LOOSE)

    assert result["pass"] is False
    assert "<f8" in result["error"] and "<f4" in result["error"]


def test_a_case_fails_and_names_a_variable_missing_from_the_submission():
    expected = {"field": np.zeros(3, dtype="<f8"), "count": np.zeros(3, dtype="<i4")}

    result = compare.compare_case(expected, {"field": np.zeros(3, dtype="<f8")}, {"field": LOOSE})

    assert result["pass"] is False
    assert result["per_var"]["count"]["pass"] is False
    assert "count" in str(result["per_var"]["count"])


def test_a_variable_nobody_expected_is_listed_but_does_not_fail_the_case():
    expected = {"field": np.zeros(3, dtype="<f8")}
    got = {"field": np.zeros(3, dtype="<f8"), "scratch": np.zeros(3, dtype="<f8")}

    result = compare.compare_case(expected, got, {"field": LOOSE})

    assert result["pass"] is True
    assert result["extra"] == ["scratch"]


def test_missing_tolerances_for_a_float_variable_is_a_refusal_not_a_pass():
    # The oracle checks this at startup; a variable that slipped through
    # must not be quietly compared with no band at all.
    with pytest.raises(KeyError):
        compare.compare_case({"field": np.zeros(3, dtype="<f8")},
                             {"field": np.zeros(3, dtype="<f8")}, {})
