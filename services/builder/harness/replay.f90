program replay

  ! Standalone replay driver. Reads ONE case's input state from a directory,
  ! applies exactly one step of the (possibly ported) kernel, and writes the
  ! output state back into the same directory. Size-agnostic: the grid size is
  ! recovered from the input file, so the same binary replays any dataset.
  !
  ! Usage: replay <case_dir>   (default '.')
  ! Reads:  <case_dir>/h_in.bin, <case_dir>/u_in.bin
  ! Writes: <case_dir>/h_out.bin, <case_dir>/u_out.bin
  !
  ! The builder runs this once per case, in a scratch directory holding only the
  ! inputs it was given -- so replay never sees, and cannot overwrite, the
  ! oracle's reference outputs.

  use iso_fortran_env, only: int32, real32
  use mod_kernel, only: step
  use mod_capture, only: dump_r32, load_r32, file_n_r32

  implicit none

  character(len=1024) :: casedir
  integer(int32) :: ngrid
  real(real32), allocatable :: h(:), u(:)

  if (command_argument_count() < 1) then
    casedir = '.'
  else
    call get_command_argument(1, casedir)
  end if

  ngrid = file_n_r32(trim(casedir) // '/h_in.bin')
  allocate(h(ngrid), u(ngrid))

  call load_r32(trim(casedir) // '/h_in.bin', h)
  call load_r32(trim(casedir) // '/u_in.bin', u)

  call step(h, u)

  call dump_r32(trim(casedir) // '/h_out.bin', h)
  call dump_r32(trim(casedir) // '/u_out.bin', u)

end program replay
