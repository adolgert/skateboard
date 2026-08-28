program replay

  ! Standalone replay driver: reads ONE case's input arrays from a directory,
  ! calls the region entry exactly once, and writes the output arrays back into
  ! the same directory. Shapes come from the input files, so the same binary
  ! replays any dataset of this code.
  !
  ! Usage: replay <case_dir>   (default '.')
  !
  ! This driver names one code's kernel and one code's variables, which is
  ! unavoidable -- a driver has to call something. It is baked into the builder
  ! image for now; a later change moves the driver into each code's own tree,
  ! written once during that code's onboarding and frozen for the port, so that
  ! the image holds no code-specific file at all.
  !
  ! The builder runs this once per case, in a scratch directory holding only the
  ! inputs it was given -- so replay never sees, and cannot overwrite, the
  ! oracle's reference outputs.

  use iso_fortran_env, only: real32
  use mod_kernel, only: step
  use npy_io, only: npy_save, npy_load

  implicit none

  character(len=1024) :: casedir
  real(real32), allocatable :: h(:), u(:)

  if (command_argument_count() < 1) then
    casedir = '.'
  else
    call get_command_argument(1, casedir)
  end if

  call npy_load(trim(casedir) // '/h.npy', h)
  call npy_load(trim(casedir) // '/u.npy', u)

  call step(h, u)

  call npy_save(trim(casedir) // '/h.out.npy', h)
  call npy_save(trim(casedir) // '/u.out.npy', u)

end program replay
