module mod_capture

  ! Minimal binary array I/O for capture-replay.
  ! Format: raw stream, little-endian float32, no header. Array length is
  ! recovered from the file size, so a reader needs no separate manifest.
  ! numpy reads a file with: np.fromfile(path, dtype='<f4').

  use iso_fortran_env, only: real32, int64
  implicit none

  private
  public :: dump_r32, load_r32, file_n_r32

contains

  subroutine dump_r32(path, x)
    character(*), intent(in) :: path
    real(real32), intent(in) :: x(:)
    integer :: u
    open(newunit=u, file=path, form='unformatted', access='stream', &
         status='replace', action='write')
    write(u) x
    close(u)
  end subroutine dump_r32

  integer function file_n_r32(path) result(n)
    ! Number of float32 values in the file (bytes / 4).
    character(*), intent(in) :: path
    integer(int64) :: nbytes
    inquire(file=path, size=nbytes)
    n = int(nbytes / 4_int64)
  end function file_n_r32

  subroutine load_r32(path, x)
    character(*), intent(in) :: path
    real(real32), intent(out) :: x(:)
    integer :: u
    open(newunit=u, file=path, form='unformatted', access='stream', &
         status='old', action='read')
    read(u) x
    close(u)
  end subroutine load_r32

end module mod_capture
