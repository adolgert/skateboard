#!/usr/bin/env python3
"""Writes npy_io.f90: the Fortran side of the capture format.

Fortran has no generics, so one procedure per element type per rank has
to exist -- twenty-five to write an array and twenty-five to read one.
They differ only in a declaration and a shape, so they are generated from
one template here rather than copied fifty times by hand, where the
fiftieth copy is where the typo lives.

The generated file is checked in beside this script and is what the
builder image bakes; the image runs no Python. Run this script after
editing the template and commit both files. A test compares the checked-in
file against what this script produces, so an edit made to the generated
file by hand is caught rather than quietly overwritten later.

Usage: python3 gen_npy_io.py [output path]
"""
from __future__ import annotations

import sys
from pathlib import Path

MAX_RANK = 4

# (suffix, Fortran declaration, NPY descr). A Fortran default logical is
# four bytes wide while NPY stores one byte per element, so logicals go
# through a one-byte integer on the way in and out; the rest are written
# straight.
TYPES = [
    ("r4", "real(real32)", "<f4"),
    ("r8", "real(real64)", "<f8"),
    ("i4", "integer(int32)", "<i4"),
    ("i8", "integer(int64)", "<i8"),
    ("l1", "logical", "|b1"),
]

HEAD = """\
module npy_io

  ! Reads and writes NumPy .npy files (format 1.0), for every element type
  ! and rank the capture format carries. One file holds one array and says
  ! for itself what type, what shape, and what element order it has, so a
  ! reader needs nothing told to it from outside.
  !
  ! Arrays are written in Fortran (column-major) order, which is what a
  ! Fortran unformatted stream write produces already, and the header says
  ! so. numpy reads such a file with np.load and hands back an array whose
  ! flags.f_contiguous is true.
  !
  !   call npy_save('<dir>/field.npy', field)      ! any type, rank 0 to 4
  !   call npy_load('<dir>/field.npy', field)      ! allocatable; allocated here
  !
  ! A load whose file holds a different element type or a different rank
  ! than the variable it was asked to fill stops the program, naming the
  ! file and the disagreement: a replay that silently reinterpreted its
  ! inputs would produce outputs that compare against the wrong answers.
  !
  ! GENERATED FILE -- do not edit. It comes from gen_npy_io.py in this
  ! directory; edit the template there and run it.

  use iso_fortran_env, only: int8, int16, int32, int64, real32, real64, error_unit
  implicit none

  private
  public :: npy_save, npy_load

  ! The longest header this reader will accept, and the deepest array.
  integer(int32), parameter :: npy_max_header = 4096
  integer(int32), parameter :: npy_max_rank = {max_rank}

  interface npy_save
    module procedure &
{save_list}
  end interface npy_save

  interface npy_load
    module procedure &
{load_list}
  end interface npy_load

contains

  subroutine npy_fail(path, reason)
    ! Every refusal in this module comes out here, so a failure always
    ! says which file and what was wrong with it before stopping.
    character(*), intent(in) :: path
    character(*), intent(in) :: reason
    write(error_unit, '(a)') 'npy_io: ' // trim(path) // ': ' // trim(reason)
    error stop 1
  end subroutine npy_fail

  function npy_header_text(descr, dims) result(header)
    ! The dictionary line NPY 1.0 puts after the ten-byte preamble,
    ! space-padded and newline-terminated so that the whole header is a
    ! multiple of 64 bytes.
    character(*), intent(in) :: descr
    integer(int32), intent(in) :: dims(:)
    character(:), allocatable :: header
    character(:), allocatable :: dict
    character(len=32) :: number
    integer(int32) :: i, total, pad

    dict = "{{'descr': '" // descr // "', 'fortran_order': True, 'shape': ("
    do i = 1, size(dims)
      write(number, '(i0)') dims(i)
      dict = dict // trim(number) // ', '
    end do
    dict = dict // '), }}'

    total = 10 + len(dict) + 1
    pad = mod(64 - mod(total, 64), 64)
    header = dict // repeat(' ', pad) // new_line('a')
  end function npy_header_text

  subroutine npy_open_write(path, descr, dims, unit)
    character(*), intent(in) :: path
    character(*), intent(in) :: descr
    integer(int32), intent(in) :: dims(:)
    integer(int32), intent(out) :: unit
    character(:), allocatable :: header
    integer(int16) :: header_len

    header = npy_header_text(descr, dims)
    header_len = int(len(header), int16)
    open(newunit=unit, file=path, form='unformatted', access='stream', &
         status='replace', action='write')
    write(unit) char(147) // 'NUMPY' // char(1) // char(0)
    write(unit) header_len
    write(unit) header
  end subroutine npy_open_write

  subroutine npy_open_read(path, want_descr, want_rank, dims, unit)
    ! Opens the file, checks it against the type and rank the caller is
    ! prepared to hold, and leaves the unit positioned at the first
    ! element. `dims` comes back holding `want_rank` extents.
    character(*), intent(in) :: path
    character(*), intent(in) :: want_descr
    integer(int32), intent(in) :: want_rank
    integer(int32), intent(out) :: dims(npy_max_rank)
    integer(int32), intent(out) :: unit

    character(len=8) :: preamble
    character(len=16) :: descr
    character(:), allocatable :: header
    character(len=96) :: message
    integer(int16) :: raw_len
    integer(int32) :: header_len, rank
    logical :: found

    inquire(file=path, exist=found)
    if (.not. found) call npy_fail(path, 'no such file')

    open(newunit=unit, file=path, form='unformatted', access='stream', &
         status='old', action='read')
    read(unit) preamble
    if (preamble(1:6) /= char(147) // 'NUMPY') call npy_fail(path, 'not an NPY file')

    read(unit) raw_len
    header_len = int(raw_len, int32)
    if (header_len < 0) header_len = header_len + 65536
    if (header_len < 1 .or. header_len > npy_max_header) &
      call npy_fail(path, 'NPY header length is out of range')
    allocate(character(len=header_len) :: header)
    read(unit) header

    call npy_parse_header(path, header, descr, dims, rank)

    if (trim(descr) /= want_descr) then
      call npy_fail(path, 'holds dtype ' // trim(descr) // ' but was read as ' // want_descr)
    end if
    if (rank /= want_rank) then
      write(message, '(a,i0,a,i0)') 'holds a rank-', rank, ' array but was read as rank ', want_rank
      call npy_fail(path, trim(message))
    end if
  end subroutine npy_open_read

  subroutine npy_parse_header(path, header, descr, dims, rank)
    character(*), intent(in) :: path
    character(*), intent(in) :: header
    character(*), intent(out) :: descr
    integer(int32), intent(out) :: dims(npy_max_rank)
    integer(int32), intent(out) :: rank

    integer(int32) :: at, open_quote, close_quote, open_paren, close_paren
    integer(int32) :: i, first, comma_at
    logical :: separator

    descr = ' '
    dims = 0
    rank = 0

    at = index(header, "'descr'")
    if (at == 0) call npy_fail(path, 'NPY header names no dtype')
    at = at + 7
    open_quote = index(header(at:), "'")
    if (open_quote == 0) call npy_fail(path, 'NPY header dtype is not quoted')
    open_quote = at + open_quote - 1
    close_quote = index(header(open_quote + 1:), "'")
    if (close_quote == 0) call npy_fail(path, 'NPY header dtype is not quoted')
    close_quote = open_quote + close_quote
    if (close_quote - open_quote - 1 > len(descr)) call npy_fail(path, 'NPY header dtype is too long')
    descr = header(open_quote + 1:close_quote - 1)

    at = index(header, "'shape'")
    if (at == 0) call npy_fail(path, 'NPY header names no shape')
    open_paren = index(header(at:), '(')
    close_paren = index(header(at:), ')')
    if (open_paren == 0 .or. close_paren <= open_paren) call npy_fail(path, 'NPY header shape is malformed')
    open_paren = at + open_paren
    close_paren = at + close_paren - 2

    first = open_paren
    do i = open_paren, close_paren + 1
      separator = .false.
      if (i > close_paren) then
        separator = .true.
      else if (header(i:i) == ',') then
        separator = .true.
      end if
      if (separator) then
        comma_at = i
        if (len_trim(header(first:comma_at - 1)) > 0) then
          rank = rank + 1
          if (rank > npy_max_rank) call npy_fail(path, 'NPY array has more dimensions than this reader holds')
          read(header(first:comma_at - 1), *) dims(rank)
        end if
        first = comma_at + 1
      end if
    end do

    ! Only rank two and above have an element order to disagree about; a
    ! vector reads the same either way, and numpy writes False for one.
    if (rank >= 2) then
      at = index(header, "'fortran_order'")
      if (at == 0) call npy_fail(path, 'NPY header does not say which element order it uses')
      if (index(header(at:), 'True') == 0) &
        call npy_fail(path, 'NPY file is in C element order; this reader reads Fortran order')
    end if
  end subroutine npy_parse_header
"""

TAIL = """
end module npy_io
"""

SAVE_PLAIN = """
  subroutine npy_save_{suffix}_{rank}(path, x)
    character(*), intent(in) :: path
    {decl}, intent(in) :: x{dummy}
    integer(int32) :: unit
    integer(int32) :: dims({rank})
    dims = int(shape(x), int32)
    call npy_open_write(path, '{descr}', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_{suffix}_{rank}
"""

SAVE_LOGICAL = """
  subroutine npy_save_{suffix}_{rank}(path, x)
    character(*), intent(in) :: path
    {decl}, intent(in) :: x{dummy}
    integer(int32) :: unit
    integer(int32) :: dims({rank})
    integer(int8){raw_decl} :: raw{raw_dummy}
    dims = int(shape(x), int32)
    call npy_open_write(path, '{descr}', dims, unit)
{raw_alloc}    raw = merge(1_int8, 0_int8, x)
    write(unit) raw
    close(unit)
  end subroutine npy_save_{suffix}_{rank}
"""

LOAD_PLAIN = """
  subroutine npy_load_{suffix}_{rank}(path, x)
    character(*), intent(in) :: path
    {decl}{alloc_attr}, intent(out) :: x{dummy}
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '{descr}', {rank}, dims, unit)
{allocate}    read(unit) x
    close(unit)
  end subroutine npy_load_{suffix}_{rank}
"""

LOAD_LOGICAL = """
  subroutine npy_load_{suffix}_{rank}(path, x)
    character(*), intent(in) :: path
    {decl}{alloc_attr}, intent(out) :: x{dummy}
    integer(int32) :: unit, dims(npy_max_rank)
    integer(int8){raw_decl} :: raw{raw_dummy}
    call npy_open_read(path, '{descr}', {rank}, dims, unit)
{allocate}{raw_alloc}    read(unit) raw
    close(unit)
    x = raw /= 0_int8
  end subroutine npy_load_{suffix}_{rank}
"""


def dummy(rank: int) -> str:
    """The dummy-argument shape: nothing for a scalar, colons otherwise."""
    return "" if rank == 0 else "(" + ",".join([":"] * rank) + ")"


def bounds(rank: int) -> str:
    return "(" + ", ".join(f"dims({i})" for i in range(1, rank + 1)) + ")"


def save_procedure(suffix: str, decl: str, descr: str, rank: int) -> str:
    common = {
        "suffix": suffix, "rank": rank, "decl": decl, "descr": descr,
        "dummy": dummy(rank),
    }
    if suffix != "l1":
        return SAVE_PLAIN.format(**common)
    return SAVE_LOGICAL.format(
        raw_decl="" if rank == 0 else ", allocatable",
        raw_dummy=dummy(rank),
        raw_alloc="" if rank == 0 else f"    allocate(raw{shape_of_x(rank)})\n",
        **common,
    )


def shape_of_x(rank: int) -> str:
    return "(" + ", ".join(f"size(x, {i})" for i in range(1, rank + 1)) + ")"


def load_procedure(suffix: str, decl: str, descr: str, rank: int) -> str:
    common = {
        "suffix": suffix, "rank": rank, "decl": decl, "descr": descr,
        "dummy": dummy(rank),
        "alloc_attr": "" if rank == 0 else ", allocatable",
        "allocate": "" if rank == 0 else f"    allocate(x{bounds(rank)})\n",
    }
    if suffix != "l1":
        return LOAD_PLAIN.format(**common)
    return LOAD_LOGICAL.format(
        raw_decl="" if rank == 0 else ", allocatable",
        raw_dummy=dummy(rank),
        raw_alloc="" if rank == 0 else f"    allocate(raw{bounds(rank)})\n",
        **common,
    )


def procedure_names(kind: str) -> list:
    return [f"npy_{kind}_{suffix}_{rank}"
            for suffix, _, _ in TYPES for rank in range(MAX_RANK + 1)]


def continued(names: list) -> str:
    """A module-procedure list as one continued Fortran statement."""
    lines = [f"    {name}" for name in names]
    return ", &\n".join(lines)


def generate() -> str:
    parts = [HEAD.format(
        max_rank=MAX_RANK,
        save_list=continued(procedure_names("save")),
        load_list=continued(procedure_names("load")),
    )]
    for suffix, decl, descr in TYPES:
        for rank in range(MAX_RANK + 1):
            parts.append(save_procedure(suffix, decl, descr, rank))
    for suffix, decl, descr in TYPES:
        for rank in range(MAX_RANK + 1):
            parts.append(load_procedure(suffix, decl, descr, rank))
    parts.append(TAIL)
    return "".join(parts)


def main(argv: list) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "npy_io.f90"
    out.write_text(generate())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
