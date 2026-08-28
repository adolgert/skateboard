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
  integer(int32), parameter :: npy_max_rank = 4

  interface npy_save
    module procedure &
    npy_save_r4_0, &
    npy_save_r4_1, &
    npy_save_r4_2, &
    npy_save_r4_3, &
    npy_save_r4_4, &
    npy_save_r8_0, &
    npy_save_r8_1, &
    npy_save_r8_2, &
    npy_save_r8_3, &
    npy_save_r8_4, &
    npy_save_i4_0, &
    npy_save_i4_1, &
    npy_save_i4_2, &
    npy_save_i4_3, &
    npy_save_i4_4, &
    npy_save_i8_0, &
    npy_save_i8_1, &
    npy_save_i8_2, &
    npy_save_i8_3, &
    npy_save_i8_4, &
    npy_save_l1_0, &
    npy_save_l1_1, &
    npy_save_l1_2, &
    npy_save_l1_3, &
    npy_save_l1_4
  end interface npy_save

  interface npy_load
    module procedure &
    npy_load_r4_0, &
    npy_load_r4_1, &
    npy_load_r4_2, &
    npy_load_r4_3, &
    npy_load_r4_4, &
    npy_load_r8_0, &
    npy_load_r8_1, &
    npy_load_r8_2, &
    npy_load_r8_3, &
    npy_load_r8_4, &
    npy_load_i4_0, &
    npy_load_i4_1, &
    npy_load_i4_2, &
    npy_load_i4_3, &
    npy_load_i4_4, &
    npy_load_i8_0, &
    npy_load_i8_1, &
    npy_load_i8_2, &
    npy_load_i8_3, &
    npy_load_i8_4, &
    npy_load_l1_0, &
    npy_load_l1_1, &
    npy_load_l1_2, &
    npy_load_l1_3, &
    npy_load_l1_4
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

    dict = "{'descr': '" // descr // "', 'fortran_order': True, 'shape': ("
    do i = 1, size(dims)
      write(number, '(i0)') dims(i)
      dict = dict // trim(number) // ', '
    end do
    dict = dict // '), }'

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

  subroutine npy_save_r4_0(path, x)
    character(*), intent(in) :: path
    real(real32), intent(in) :: x
    integer(int32) :: unit
    integer(int32) :: dims(0)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r4_0

  subroutine npy_save_r4_1(path, x)
    character(*), intent(in) :: path
    real(real32), intent(in) :: x(:)
    integer(int32) :: unit
    integer(int32) :: dims(1)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r4_1

  subroutine npy_save_r4_2(path, x)
    character(*), intent(in) :: path
    real(real32), intent(in) :: x(:,:)
    integer(int32) :: unit
    integer(int32) :: dims(2)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r4_2

  subroutine npy_save_r4_3(path, x)
    character(*), intent(in) :: path
    real(real32), intent(in) :: x(:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(3)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r4_3

  subroutine npy_save_r4_4(path, x)
    character(*), intent(in) :: path
    real(real32), intent(in) :: x(:,:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(4)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r4_4

  subroutine npy_save_r8_0(path, x)
    character(*), intent(in) :: path
    real(real64), intent(in) :: x
    integer(int32) :: unit
    integer(int32) :: dims(0)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r8_0

  subroutine npy_save_r8_1(path, x)
    character(*), intent(in) :: path
    real(real64), intent(in) :: x(:)
    integer(int32) :: unit
    integer(int32) :: dims(1)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r8_1

  subroutine npy_save_r8_2(path, x)
    character(*), intent(in) :: path
    real(real64), intent(in) :: x(:,:)
    integer(int32) :: unit
    integer(int32) :: dims(2)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r8_2

  subroutine npy_save_r8_3(path, x)
    character(*), intent(in) :: path
    real(real64), intent(in) :: x(:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(3)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r8_3

  subroutine npy_save_r8_4(path, x)
    character(*), intent(in) :: path
    real(real64), intent(in) :: x(:,:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(4)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<f8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_r8_4

  subroutine npy_save_i4_0(path, x)
    character(*), intent(in) :: path
    integer(int32), intent(in) :: x
    integer(int32) :: unit
    integer(int32) :: dims(0)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i4_0

  subroutine npy_save_i4_1(path, x)
    character(*), intent(in) :: path
    integer(int32), intent(in) :: x(:)
    integer(int32) :: unit
    integer(int32) :: dims(1)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i4_1

  subroutine npy_save_i4_2(path, x)
    character(*), intent(in) :: path
    integer(int32), intent(in) :: x(:,:)
    integer(int32) :: unit
    integer(int32) :: dims(2)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i4_2

  subroutine npy_save_i4_3(path, x)
    character(*), intent(in) :: path
    integer(int32), intent(in) :: x(:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(3)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i4_3

  subroutine npy_save_i4_4(path, x)
    character(*), intent(in) :: path
    integer(int32), intent(in) :: x(:,:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(4)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i4', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i4_4

  subroutine npy_save_i8_0(path, x)
    character(*), intent(in) :: path
    integer(int64), intent(in) :: x
    integer(int32) :: unit
    integer(int32) :: dims(0)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i8_0

  subroutine npy_save_i8_1(path, x)
    character(*), intent(in) :: path
    integer(int64), intent(in) :: x(:)
    integer(int32) :: unit
    integer(int32) :: dims(1)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i8_1

  subroutine npy_save_i8_2(path, x)
    character(*), intent(in) :: path
    integer(int64), intent(in) :: x(:,:)
    integer(int32) :: unit
    integer(int32) :: dims(2)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i8_2

  subroutine npy_save_i8_3(path, x)
    character(*), intent(in) :: path
    integer(int64), intent(in) :: x(:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(3)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i8_3

  subroutine npy_save_i8_4(path, x)
    character(*), intent(in) :: path
    integer(int64), intent(in) :: x(:,:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(4)
    dims = int(shape(x), int32)
    call npy_open_write(path, '<i8', dims, unit)
    write(unit) x
    close(unit)
  end subroutine npy_save_i8_4

  subroutine npy_save_l1_0(path, x)
    character(*), intent(in) :: path
    logical, intent(in) :: x
    integer(int32) :: unit
    integer(int32) :: dims(0)
    integer(int8) :: raw
    dims = int(shape(x), int32)
    call npy_open_write(path, '|b1', dims, unit)
    raw = merge(1_int8, 0_int8, x)
    write(unit) raw
    close(unit)
  end subroutine npy_save_l1_0

  subroutine npy_save_l1_1(path, x)
    character(*), intent(in) :: path
    logical, intent(in) :: x(:)
    integer(int32) :: unit
    integer(int32) :: dims(1)
    integer(int8), allocatable :: raw(:)
    dims = int(shape(x), int32)
    call npy_open_write(path, '|b1', dims, unit)
    allocate(raw(size(x, 1)))
    raw = merge(1_int8, 0_int8, x)
    write(unit) raw
    close(unit)
  end subroutine npy_save_l1_1

  subroutine npy_save_l1_2(path, x)
    character(*), intent(in) :: path
    logical, intent(in) :: x(:,:)
    integer(int32) :: unit
    integer(int32) :: dims(2)
    integer(int8), allocatable :: raw(:,:)
    dims = int(shape(x), int32)
    call npy_open_write(path, '|b1', dims, unit)
    allocate(raw(size(x, 1), size(x, 2)))
    raw = merge(1_int8, 0_int8, x)
    write(unit) raw
    close(unit)
  end subroutine npy_save_l1_2

  subroutine npy_save_l1_3(path, x)
    character(*), intent(in) :: path
    logical, intent(in) :: x(:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(3)
    integer(int8), allocatable :: raw(:,:,:)
    dims = int(shape(x), int32)
    call npy_open_write(path, '|b1', dims, unit)
    allocate(raw(size(x, 1), size(x, 2), size(x, 3)))
    raw = merge(1_int8, 0_int8, x)
    write(unit) raw
    close(unit)
  end subroutine npy_save_l1_3

  subroutine npy_save_l1_4(path, x)
    character(*), intent(in) :: path
    logical, intent(in) :: x(:,:,:,:)
    integer(int32) :: unit
    integer(int32) :: dims(4)
    integer(int8), allocatable :: raw(:,:,:,:)
    dims = int(shape(x), int32)
    call npy_open_write(path, '|b1', dims, unit)
    allocate(raw(size(x, 1), size(x, 2), size(x, 3), size(x, 4)))
    raw = merge(1_int8, 0_int8, x)
    write(unit) raw
    close(unit)
  end subroutine npy_save_l1_4

  subroutine npy_load_r4_0(path, x)
    character(*), intent(in) :: path
    real(real32), intent(out) :: x
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f4', 0, dims, unit)
    read(unit) x
    close(unit)
  end subroutine npy_load_r4_0

  subroutine npy_load_r4_1(path, x)
    character(*), intent(in) :: path
    real(real32), allocatable, intent(out) :: x(:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f4', 1, dims, unit)
    allocate(x(dims(1)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r4_1

  subroutine npy_load_r4_2(path, x)
    character(*), intent(in) :: path
    real(real32), allocatable, intent(out) :: x(:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f4', 2, dims, unit)
    allocate(x(dims(1), dims(2)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r4_2

  subroutine npy_load_r4_3(path, x)
    character(*), intent(in) :: path
    real(real32), allocatable, intent(out) :: x(:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f4', 3, dims, unit)
    allocate(x(dims(1), dims(2), dims(3)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r4_3

  subroutine npy_load_r4_4(path, x)
    character(*), intent(in) :: path
    real(real32), allocatable, intent(out) :: x(:,:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f4', 4, dims, unit)
    allocate(x(dims(1), dims(2), dims(3), dims(4)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r4_4

  subroutine npy_load_r8_0(path, x)
    character(*), intent(in) :: path
    real(real64), intent(out) :: x
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f8', 0, dims, unit)
    read(unit) x
    close(unit)
  end subroutine npy_load_r8_0

  subroutine npy_load_r8_1(path, x)
    character(*), intent(in) :: path
    real(real64), allocatable, intent(out) :: x(:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f8', 1, dims, unit)
    allocate(x(dims(1)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r8_1

  subroutine npy_load_r8_2(path, x)
    character(*), intent(in) :: path
    real(real64), allocatable, intent(out) :: x(:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f8', 2, dims, unit)
    allocate(x(dims(1), dims(2)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r8_2

  subroutine npy_load_r8_3(path, x)
    character(*), intent(in) :: path
    real(real64), allocatable, intent(out) :: x(:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f8', 3, dims, unit)
    allocate(x(dims(1), dims(2), dims(3)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r8_3

  subroutine npy_load_r8_4(path, x)
    character(*), intent(in) :: path
    real(real64), allocatable, intent(out) :: x(:,:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<f8', 4, dims, unit)
    allocate(x(dims(1), dims(2), dims(3), dims(4)))
    read(unit) x
    close(unit)
  end subroutine npy_load_r8_4

  subroutine npy_load_i4_0(path, x)
    character(*), intent(in) :: path
    integer(int32), intent(out) :: x
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i4', 0, dims, unit)
    read(unit) x
    close(unit)
  end subroutine npy_load_i4_0

  subroutine npy_load_i4_1(path, x)
    character(*), intent(in) :: path
    integer(int32), allocatable, intent(out) :: x(:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i4', 1, dims, unit)
    allocate(x(dims(1)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i4_1

  subroutine npy_load_i4_2(path, x)
    character(*), intent(in) :: path
    integer(int32), allocatable, intent(out) :: x(:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i4', 2, dims, unit)
    allocate(x(dims(1), dims(2)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i4_2

  subroutine npy_load_i4_3(path, x)
    character(*), intent(in) :: path
    integer(int32), allocatable, intent(out) :: x(:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i4', 3, dims, unit)
    allocate(x(dims(1), dims(2), dims(3)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i4_3

  subroutine npy_load_i4_4(path, x)
    character(*), intent(in) :: path
    integer(int32), allocatable, intent(out) :: x(:,:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i4', 4, dims, unit)
    allocate(x(dims(1), dims(2), dims(3), dims(4)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i4_4

  subroutine npy_load_i8_0(path, x)
    character(*), intent(in) :: path
    integer(int64), intent(out) :: x
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i8', 0, dims, unit)
    read(unit) x
    close(unit)
  end subroutine npy_load_i8_0

  subroutine npy_load_i8_1(path, x)
    character(*), intent(in) :: path
    integer(int64), allocatable, intent(out) :: x(:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i8', 1, dims, unit)
    allocate(x(dims(1)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i8_1

  subroutine npy_load_i8_2(path, x)
    character(*), intent(in) :: path
    integer(int64), allocatable, intent(out) :: x(:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i8', 2, dims, unit)
    allocate(x(dims(1), dims(2)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i8_2

  subroutine npy_load_i8_3(path, x)
    character(*), intent(in) :: path
    integer(int64), allocatable, intent(out) :: x(:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i8', 3, dims, unit)
    allocate(x(dims(1), dims(2), dims(3)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i8_3

  subroutine npy_load_i8_4(path, x)
    character(*), intent(in) :: path
    integer(int64), allocatable, intent(out) :: x(:,:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    call npy_open_read(path, '<i8', 4, dims, unit)
    allocate(x(dims(1), dims(2), dims(3), dims(4)))
    read(unit) x
    close(unit)
  end subroutine npy_load_i8_4

  subroutine npy_load_l1_0(path, x)
    character(*), intent(in) :: path
    logical, intent(out) :: x
    integer(int32) :: unit, dims(npy_max_rank)
    integer(int8) :: raw
    call npy_open_read(path, '|b1', 0, dims, unit)
    read(unit) raw
    close(unit)
    x = raw /= 0_int8
  end subroutine npy_load_l1_0

  subroutine npy_load_l1_1(path, x)
    character(*), intent(in) :: path
    logical, allocatable, intent(out) :: x(:)
    integer(int32) :: unit, dims(npy_max_rank)
    integer(int8), allocatable :: raw(:)
    call npy_open_read(path, '|b1', 1, dims, unit)
    allocate(x(dims(1)))
    allocate(raw(dims(1)))
    read(unit) raw
    close(unit)
    x = raw /= 0_int8
  end subroutine npy_load_l1_1

  subroutine npy_load_l1_2(path, x)
    character(*), intent(in) :: path
    logical, allocatable, intent(out) :: x(:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    integer(int8), allocatable :: raw(:,:)
    call npy_open_read(path, '|b1', 2, dims, unit)
    allocate(x(dims(1), dims(2)))
    allocate(raw(dims(1), dims(2)))
    read(unit) raw
    close(unit)
    x = raw /= 0_int8
  end subroutine npy_load_l1_2

  subroutine npy_load_l1_3(path, x)
    character(*), intent(in) :: path
    logical, allocatable, intent(out) :: x(:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    integer(int8), allocatable :: raw(:,:,:)
    call npy_open_read(path, '|b1', 3, dims, unit)
    allocate(x(dims(1), dims(2), dims(3)))
    allocate(raw(dims(1), dims(2), dims(3)))
    read(unit) raw
    close(unit)
    x = raw /= 0_int8
  end subroutine npy_load_l1_3

  subroutine npy_load_l1_4(path, x)
    character(*), intent(in) :: path
    logical, allocatable, intent(out) :: x(:,:,:,:)
    integer(int32) :: unit, dims(npy_max_rank)
    integer(int8), allocatable :: raw(:,:,:,:)
    call npy_open_read(path, '|b1', 4, dims, unit)
    allocate(x(dims(1), dims(2), dims(3), dims(4)))
    allocate(raw(dims(1), dims(2), dims(3), dims(4)))
    read(unit) raw
    close(unit)
    x = raw /= 0_int8
  end subroutine npy_load_l1_4

end module npy_io
