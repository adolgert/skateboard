"""The Fortran writer and the Python reader agree, for every type and rank.

A Fortran program writes one array of each element type at each rank and
reads every one of them back; then this test opens the same files with
the Python reader. Both halves have to agree about element order, or a
port would be compared against a transposed reference.

The Fortran source is generated, so this also asserts that the file
checked in beside the generator is what the generator produces -- an
edit made to the generated file by hand would otherwise survive until the
next person regenerated it.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HARNESS = Path(__file__).resolve().parents[3] / "services" / "builder" / "harness"
sys.path.insert(0, str(HARNESS))
import gen_npy_io  # noqa: E402  (found through the path added just above)

from equivalent.capture import npy  # noqa: E402

# One shape per rank, with unequal extents so a column-major array read as
# row-major comes back the wrong shape rather than merely transposed.
SHAPES = {0: (), 1: (5,), 2: (2, 3), 3: (2, 3, 4), 4: (2, 3, 4, 5)}

# (generator suffix, Fortran declaration, manifest dtype)
KINDS = [
    ("r4", "real(real32)", "f32"),
    ("r8", "real(real64)", "f64"),
    ("i4", "integer(int32)", "i32"),
    ("i8", "integer(int64)", "i64"),
    ("l1", "logical", "l"),
]

needs_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None, reason="gfortran is not installed here"
)


def test_the_checked_in_fortran_is_what_the_generator_produces():
    assert (HARNESS / "npy_io.f90").read_text() == gen_npy_io.generate()


# The one value a rank-0 array carries. Any value would do; this one is
# distinct from every element index, so a scalar read as a vector shows up.
SCALAR = 7


def _expected(dtype: str, rank: int) -> np.ndarray:
    shape = SHAPES[rank]
    n = int(np.prod(shape, dtype=int))
    values = np.array([SCALAR]) if rank == 0 else np.arange(1, n + 1)
    if dtype == "l":
        values = (values % 3) == 0
    return np.asarray(values, dtype=npy.NUMPY_DTYPE[dtype]).reshape(shape, order="F")


def _fortran_value(suffix: str, index: str) -> str:
    """The i-th element, spelled in Fortran so it matches _expected."""
    if suffix == "l1":
        return f"mod({index}, 3) == 0"
    if suffix.startswith("r"):
        kind = "real32" if suffix == "r4" else "real64"
        return f"real({index}, {kind})"
    kind = "int32" if suffix == "i4" else "int64"
    return f"int({index}, {kind})"


def _program(directory: Path) -> str:
    """A Fortran program that writes one array of every type and rank, then
    reads each back and prints whether the values survived the trip."""
    lines = [
        "program npy_round_trip",
        "  use iso_fortran_env, only: int32, int64, real32, real64",
        "  use npy_io, only: npy_save, npy_load",
        "  implicit none",
        f"  character(*), parameter :: d = '{directory}'",
        "  integer(int32) :: i",
    ]
    for suffix, decl, _ in KINDS:
        for rank in SHAPES:
            shape = "" if rank == 0 else "(" + ",".join([":"] * rank) + ")"
            lines.append(f"  {decl} :: a_{suffix}_{rank}{shape.replace(':', '')}"
                         if rank == 0 else
                         f"  {decl}, allocatable :: a_{suffix}_{rank}{shape}")
            lines.append(f"  {decl}, allocatable :: b_{suffix}_{rank}{shape}"
                         if rank else f"  {decl} :: b_{suffix}_{rank}")
    for suffix, _, _ in KINDS:
        for rank, shape in SHAPES.items():
            name = f"a_{suffix}_{rank}"
            path = f"{name}.npy"
            if rank == 0:
                lines.append(f"  {name} = {_fortran_value(suffix, str(SCALAR))}")
            else:
                extents = ", ".join(str(n) for n in shape)
                n = int(np.prod(shape, dtype=int))
                lines.append(f"  allocate({name}({extents}))")
                lines.append(
                    f"  {name} = reshape([({_fortran_value(suffix, 'i')}, i = 1, {n})], "
                    f"[{extents}])"
                )
            lines.append(f"  call npy_save(d // '/{path}', {name})")
            lines.append(f"  call npy_load(d // '/{path}', b_{suffix}_{rank})")
            same = f"b_{suffix}_{rank} .eqv. {name}" if suffix == "l1" and rank == 0 else (
                f"all(b_{suffix}_{rank} .eqv. {name})" if suffix == "l1" else
                f"b_{suffix}_{rank} == {name}" if rank == 0 else
                f"all(b_{suffix}_{rank} == {name})"
            )
            lines.append(f"  print '(a,1x,l1)', '{name}', {same}")
    lines.append("end program npy_round_trip")
    return "\n".join(lines) + "\n"


@needs_gfortran
def test_every_type_and_rank_survives_the_trip_through_a_file(tmp_path):
    source = tmp_path / "round_trip.f90"
    source.write_text(_program(tmp_path))
    subprocess.run(
        ["gfortran", "-O1", "-std=f2008", "-J", str(tmp_path), "-o", str(tmp_path / "round_trip"),
         str(HARNESS / "npy_io.f90"), str(source)],
        check=True, capture_output=True, text=True, cwd=tmp_path,
    )
    done = subprocess.run([str(tmp_path / "round_trip")], check=True,
                          capture_output=True, text=True, cwd=tmp_path)

    read_back = dict(line.split() for line in done.stdout.splitlines())
    for suffix, _, dtype in KINDS:
        for rank in SHAPES:
            name = f"a_{suffix}_{rank}"
            expected = _expected(dtype, rank)

            assert read_back[name] == "T", f"{name}: Fortran read back something else"

            got = npy.decode((tmp_path / f"{name}.npy").read_bytes())
            assert got.dtype == np.dtype(npy.NUMPY_DTYPE[dtype]), name
            assert got.shape == expected.shape, name
            assert np.array_equal(got, expected), name
            assert got.flags.f_contiguous, name


@needs_gfortran
def test_reading_a_file_of_the_wrong_type_stops_the_program(tmp_path):
    # The refusal names the file and both dtypes, so a mismatched capture
    # set is diagnosable from the replay's own output.
    (tmp_path / "field.npy").write_bytes(npy.encode(_expected("f64", 1)))
    source = tmp_path / "wrong_type.f90"
    source.write_text(
        "program wrong_type\n"
        "  use iso_fortran_env, only: real32\n"
        "  use npy_io, only: npy_load\n"
        "  implicit none\n"
        "  real(real32), allocatable :: field(:)\n"
        f"  call npy_load('{tmp_path}/field.npy', field)\n"
        "end program wrong_type\n"
    )
    subprocess.run(
        ["gfortran", "-O1", "-std=f2008", "-J", str(tmp_path), "-o", str(tmp_path / "wrong_type"),
         str(HARNESS / "npy_io.f90"), str(source)],
        check=True, capture_output=True, text=True, cwd=tmp_path,
    )

    done = subprocess.run([str(tmp_path / "wrong_type")], capture_output=True, text=True, cwd=tmp_path)

    assert done.returncode != 0
    assert "field.npy" in done.stderr
    assert "<f8" in done.stderr and "<f4" in done.stderr
