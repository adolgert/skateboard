program gen_reference

  ! Reference (ground-truth) generator. Runs the FULL simulation on the CPU
  ! using the pristine kernel, and snapshots a handful of single-step cases:
  ! for each captured step, the state going in and the state one step later.
  ! Those snapshots are the oracle's expected answers.
  !
  ! Usage: gen_reference <grid_size> <num_steps> <icenter> <decay> <outdir>
  ! Emits: <outdir>/caseNNNN/{h,u}.npy, {h,u}.out.npy, and case.json naming
  !        them  (5 evenly spaced steps)

  use iso_fortran_env, only: int32, real32
  use mod_initial, only: set_gaussian
  use mod_kernel, only: step
  use npy_io, only: npy_save

  implicit none

  integer(int32) :: n, j, ncase, grid_size, num_steps, icenter, unit
  real(real32) :: decay
  character(len=512) :: arg, outdir, casedir
  integer(int32) :: capsteps(5)
  real(real32), allocatable :: h(:), u(:)

  call get_command_argument(1, arg); read(arg, *) grid_size
  call get_command_argument(2, arg); read(arg, *) num_steps
  call get_command_argument(3, arg); read(arg, *) icenter
  call get_command_argument(4, arg); read(arg, *) decay
  call get_command_argument(5, outdir)

  allocate(h(grid_size), u(grid_size))
  call set_gaussian(h, icenter, decay)
  u = 0

  ! five evenly spaced capture steps
  do j = 1, 5
    capsteps(j) = (num_steps / 5) * j
  end do

  ncase = 0
  do n = 1, num_steps
    if (any(capsteps == n)) then
      write(casedir, '(a,"/case",i4.4)') trim(outdir), ncase
      call execute_command_line('mkdir -p ' // trim(casedir))
      call npy_save(trim(casedir) // '/h.npy', h)
      call npy_save(trim(casedir) // '/u.npy', u)
      call step(h, u)
      call npy_save(trim(casedir) // '/h.out.npy', h)
      call npy_save(trim(casedir) // '/u.out.npy', u)
      ! What this directory holds, so a reader needs no list from elsewhere.
      open(newunit=unit, file=trim(casedir) // '/case.json', status='replace', action='write')
      write(unit, '(a)') '{"inputs": ["h", "u"], "outputs": ["h", "u"]}'
      close(unit)
      ncase = ncase + 1
    else
      call step(h, u)
    end if
  end do

  print '(a,i0,a,a)', 'generated ', ncase, ' cases in ', trim(outdir)

end program gen_reference
